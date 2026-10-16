"""Pure cursor snapshot validation against committed tool calls and results."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from tracefix.messages import Message, MessageHistory, MessageRole
from tracefix.repository_rules.domain import digest
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.state import decode_unique_json
from tracefix.tool_arguments import RuleSearchArguments

Sha = Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]
CursorId = Annotated[StrictStr, Field(pattern=r"^rules_cursor_[a-f0-9]{32}$")]
MAX_SEARCH_CHECKPOINT_BYTES = 256 * 1024


class SavedCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: StrictStr = Field(min_length=1, max_length=200)
    query_sha256: Sha
    candidates_sha256: Sha
    position: StrictInt = Field(ge=0, lt=5000)
    line_offset: StrictInt = Field(ge=0, le=1_000_000)
    file_sha256: Sha | None
    prefix_count: StrictInt = Field(ge=0, le=5000)
    rules_sha256: Sha


class SavedSearch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: StrictStr = Field(min_length=1, max_length=200)
    limit: StrictInt = Field(ge=1, le=128)
    cursors: dict[CursorId, SavedCursor] = Field(max_length=128)


class SearchEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt = Field(ge=1, le=1)
    payload: SavedSearch
    payload_sha256: Sha


def validate_search(value: object, history: Sequence[Message], *, run_id: str,
                    cursor_limit: int) -> SavedSearch:
    try:
        envelope = SearchEnvelope.model_validate(value)
        state = envelope.payload
        if envelope.payload_sha256 != digest(state.model_dump(mode="json")):
            raise RuleError("rules_search_checkpoint_digest_mismatch")
        if len(envelope.model_dump_json().encode("utf-8")) > MAX_SEARCH_CHECKPOINT_BYTES:
            raise RuleError("rules_search_checkpoint_size_limit")
        if state.run_id != run_id or state.limit != cursor_limit:
            raise RuleError("rules_search_checkpoint_identity_mismatch")
        if MessageHistory(history).pending_tool_call_ids:
            raise RuleError("rules_search_checkpoint_batch_not_closed")
        calls = {call.id: call for message in history for call in message.tool_calls}
        committed = {}
        for message in history:
            call = calls.get(message.tool_call_id)
            if message.role is not MessageRole.TOOL or call is None or call.name != "search_code":
                continue
            result = decode_unique_json(message.content or "null")
            if not isinstance(result, dict) or not isinstance(result.get("output"), dict):
                continue
            output = result["output"]
            cursor = output.get("cursor")
            if cursor is None:
                continue
            if (result.get("call_id") != call.id or result.get("tool_name") != "search_code"
                    or result.get("success") is not True or cursor in committed):
                raise RuleError("rules_search_checkpoint_result_reference_invalid")
            selected = state.cursors.get(cursor)
            if selected is None:
                raise RuleError("rules_search_checkpoint_unregistered_cursor")
            arguments = RuleSearchArguments.model_validate(call.arguments)
            query_sha = digest(arguments.model_dump(exclude={"cursor"}))
            continuation = output.get("continuation")
            if (not isinstance(continuation, dict) or continuation.get("tool_name") != "search_code"
                    or not isinstance(continuation.get("arguments"), dict)):
                raise RuleError("rules_search_checkpoint_continuation_invalid")
            resumed = RuleSearchArguments.model_validate(continuation["arguments"])
            metadata = result.get("metadata")
            if (resumed.cursor != cursor
                    or digest(resumed.model_dump(exclude={"cursor"})) != query_sha
                    or not isinstance(metadata, dict)
                    or metadata.get("per_candidate_rules") is not True
                    or metadata.get("repository_search_cursor_sha256")
                    != digest(selected.model_dump(mode="json"))):
                raise RuleError("rules_search_checkpoint_cursor_proof_invalid")
            if (selected.run_id != run_id or selected.query_sha256 != query_sha
                    or selected.prefix_count not in {selected.position, selected.position + 1}
                    or bool(selected.line_offset) != (selected.file_sha256 is not None)
                    or selected.line_offset and selected.prefix_count != selected.position + 1):
                raise RuleError("rules_search_checkpoint_cursor_invalid")
            committed[cursor] = True
        if set(committed) != set(state.cursors):
            raise RuleError("rules_search_checkpoint_cursor_reference_missing")
        return state
    except (ValidationError, ValueError, TypeError, RecursionError) as exc:
        if isinstance(exc, RuleError):
            raise
        raise RuleError("rules_search_checkpoint_malformed") from exc
