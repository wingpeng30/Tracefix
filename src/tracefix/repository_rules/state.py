"""Pure validation of canonical rule delivery snapshots and history references."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

from tracefix.messages import Message, MessageHistory, MessageRole
from tracefix.repository_rules.domain import (
    MAX_SELECTION_BYTES,
    RuleDocument,
    RuleSelection,
    digest,
)
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.paths import relative_path
from tracefix.tools.base import ToolSpec

MAX_RULE_CHECKPOINT_BYTES = 16 * 1024 * 1024
Sha = Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]
MessageId = Annotated[StrictStr, Field(min_length=1, max_length=200)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SavedReceipt(_Frozen):
    request_id: Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{32}$")]
    projection_sha256: Sha
    message_ids: tuple[MessageId, ...] = Field(max_length=128)
    history_prefix_length: StrictInt = Field(ge=0)
    history_prefix_sha256: Sha
    tools_sha256: Sha
    response_message_id: MessageId


class SavedDelivery(_Frozen):
    observed: dict[StrictStr, Sha] = Field(max_length=5000)
    changes: dict[StrictStr, tuple[Sha, Sha]] = Field(max_length=5000)
    messages: tuple[Message, ...] = Field(max_length=128)
    receipt: SavedReceipt | None


class DeliveryEnvelope(_Frozen):
    schema_version: StrictInt = Field(ge=1, le=1)
    payload: SavedDelivery
    payload_sha256: Sha


class _Binding(_Frozen):
    target_path: StrictStr
    selection_sha256: Sha
    case_sensitive: StrictBool
    selection: dict
    supersedes_selection_sha256: Sha | None


class _Invalidated(_Frozen):
    rule_id: Annotated[StrictStr, Field(pattern=r"^rule_[a-f0-9]{64}$")]
    version: Sha
    source_path: StrictStr
    scope_path: StrictStr
    patterns: tuple[StrictStr, ...]


class _Guidance(_Frozen):
    schema_version: StrictInt = Field(ge=1, le=1)
    bundle_sha256: Sha
    notice: StrictStr = Field(min_length=1, max_length=2000)
    bindings: tuple[_Binding, ...] = Field(min_length=1, max_length=5000)
    invalidated_documents: tuple[_Invalidated, ...] = Field(max_length=2048)
    documents: tuple[RuleDocument, ...] = Field(max_length=16)


@dataclass(frozen=True)
class ValidatedRuleRecord:
    message: Message
    bundle_sha256: str
    documents: tuple[RuleDocument, ...]
    invalidated: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ValidatedDelivery:
    state: SavedDelivery
    records: tuple[ValidatedRuleRecord, ...]


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise RuleError("rules_checkpoint_duplicate_json_key")
        value[key] = item
    return value


def decode_unique_json(value: str):
    return json.loads(value, object_pairs_hook=_unique_object)


def validate_delivery(value: object, history: Sequence[Message],
                      tools: Sequence[ToolSpec]) -> ValidatedDelivery:
    """No live rule files, source readers or execution backends are consulted."""
    try:
        envelope = DeliveryEnvelope.model_validate(value)
        state = envelope.payload
        if envelope.payload_sha256 != digest(state.model_dump(mode="json")):
            raise RuleError("rules_checkpoint_digest_mismatch")
        if len(envelope.model_dump_json().encode("utf-8")) > MAX_RULE_CHECKPOINT_BYTES:
            raise RuleError("rules_checkpoint_size_limit")
        if MessageHistory(history).pending_tool_call_ids:
            raise RuleError("rules_checkpoint_history_not_closed")
        positions = {message.id: index for index, message in enumerate(history)}
        if len(positions) != len(history):
            raise RuleError("rules_checkpoint_duplicate_message_id")
        for path in state.observed:
            relative_path(path)
        for path, change in state.changes.items():
            if state.observed.get(path) != change[0]:
                raise RuleError("rules_checkpoint_observation_mismatch")
        records, known, order = [], {}, []
        for message in state.messages:
            position = positions.get(message.id)
            if position is None or message != history[position]:
                raise RuleError("rules_checkpoint_message_reference_invalid")
            order.append(position)
            if message.role is not MessageRole.USER or message.tool_calls:
                raise RuleError("rules_checkpoint_guidance_role_invalid")
            if len((message.content or "").encode("utf-8")) > 256 * 1024:
                raise RuleError("rules_checkpoint_message_limit")
            payload = _Guidance.model_validate(decode_unique_json(message.content or "null"))
            refs = [(doc.rule_id, doc.version) for doc in payload.documents]
            if (payload.bundle_sha256 != digest(refs)
                    or sum(len(doc.body.encode("utf-8")) for doc in payload.documents)
                    > MAX_SELECTION_BYTES):
                raise RuleError("rules_checkpoint_bundle_invalid")
            if message.metadata != {
                "kind": "repository_rules", "schema_version": 1,
                "bundle_sha256": payload.bundle_sha256,
                "content_sha256": digest(message.content), "trust": "repository_guidance",
            } or type(message.metadata.get("schema_version")) is not int:
                raise RuleError("rules_checkpoint_guidance_metadata_invalid")
            targets = set()
            for binding in payload.bindings:
                if (binding.target_path in targets or binding.target_path not in state.observed
                        or set(binding.selection) != {"schema_version", "target_path",
                            "case_sensitive", "bundle_sha256", "sources"}):
                    raise RuleError("rules_checkpoint_binding_invalid")
                selected = RuleSelection.model_validate({**binding.selection,
                    "documents": payload.documents, "sha256": binding.selection_sha256})
                if (selected.target_path != binding.target_path
                        or selected.case_sensitive != binding.case_sensitive
                        or selected.bundle_sha256 != payload.bundle_sha256):
                    raise RuleError("rules_checkpoint_binding_invalid")
                targets.add(binding.target_path)
            invalidated = []
            for ref in payload.invalidated_documents:
                key = (ref.rule_id, ref.version)
                original = known.get(key)
                if (original is None or key in invalidated or key in refs
                        or original.source_path != ref.source_path
                        or original.scope_path != ref.scope_path
                        or original.patterns != ref.patterns
                        or not any(original.applies_to(binding.target_path,
                                                       case_sensitive=binding.case_sensitive)
                                   for binding in payload.bindings)):
                    raise RuleError("rules_checkpoint_invalidation_invalid")
                invalidated.append(key)
            for document in payload.documents:
                known[(document.rule_id, document.version)] = document
            records.append(ValidatedRuleRecord(message, payload.bundle_sha256,
                                                payload.documents, tuple(invalidated)))
        if order != sorted(set(order)):
            raise RuleError("rules_checkpoint_record_order_invalid")
        expected_ids = {message.id for message in state.messages}
        history_ids = {message.id for message in history if message.role is MessageRole.USER
                       and message.metadata.get("kind") == "repository_rules"}
        if history_ids != expected_ids:
            raise RuleError("rules_checkpoint_unregistered_history_guidance")
        receipt = state.receipt
        if receipt is not None:
            length = receipt.history_prefix_length
            ticket = receipt.model_dump(mode="json", exclude={"response_message_id"})
            if (length >= len(history) or history[length].role is not MessageRole.ASSISTANT
                    or history[length].id != receipt.response_message_id
                    or digest([message.model_dump(mode="json") for message in history[:length]])
                    != receipt.history_prefix_sha256
                    or receipt.tools_sha256 != digest([
                        tool.model_dump(mode="json") for tool in tools])
                    or digest(history[length].metadata.get("repository_rules_request"))
                    != digest(ticket)
                    or len(set(receipt.message_ids)) != len(receipt.message_ids)
                    or not set(receipt.message_ids) <= expected_ids
                    or any(positions[key] >= length for key in receipt.message_ids)
                    or [positions[key] for key in receipt.message_ids]
                    != sorted(positions[key] for key in receipt.message_ids)):
                raise RuleError("rules_checkpoint_receipt_reference_invalid")
        return ValidatedDelivery(state, tuple(records))
    except (ValidationError, ValueError, RecursionError) as exc:
        if isinstance(exc, RuleError):
            raise
        raise RuleError("rules_checkpoint_malformed") from exc
