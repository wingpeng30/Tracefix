"""Typed, bounded task-state projection and recovery contracts.

This module is independent of the runner, model, tool registry, and execution backend.
Host adapters pass already classified events and verified direct-user receipts.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

TASK_STATE_SCHEMA_VERSION = 1
MAX_TASK_STATE_BYTES = 256 * 1024
MAX_TASK_STATE_SUMMARY_BYTES = 8 * 1024
MAX_TASK_REQUIREMENT_CHARS = 20_000
MAX_TASK_ITEM_CHARS = 1_000
MAX_TASK_STATE_OPERATIONS = 16
MAX_TASK_STATE_ITEMS = 128
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class TaskStateError(ValueError):
    """Task state is malformed, over limit, or references uncommitted history."""


class TaskStateRevisionConflict(TaskStateError):
    """A model update used a stale state revision."""


class ObservationValidity(StrEnum):
    VALID = "valid"
    HISTORICAL = "historical"
    STALE = "stale"


class TaskItemStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    OPEN = "open"
    DONE = "done"
    BLOCKED = "blocked"
    RESOLVED = "resolved"


class TaskRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str = Field(min_length=1, max_length=200)
    message_id: str = Field(min_length=1, max_length=200)
    receipt_id: str = Field(min_length=1, max_length=200)
    turn: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=MAX_TASK_REQUIREMENT_CHARS)
    status: Literal["active", "superseded"] = "active"
    superseded_by: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_supersession(self) -> TaskRequirement:
        if bool(self.superseded_by) != (self.status == "superseded"):
            raise ValueError("task requirement supersession fields disagree")
        if not self.text.strip():
            raise ValueError("task requirement cannot be blank")
        return self


class TaskAcceptanceCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str = Field(min_length=1, max_length=256)
    source: Literal["run_config", "user_requirement"]
    text: str = Field(min_length=1, max_length=MAX_TASK_ITEM_CHARS)
    message_id: str | None = Field(default=None, max_length=200)


class TaskObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=MAX_TASK_ITEM_CHARS)
    source_message_id: str = Field(min_length=1, max_length=200)
    source_call_id: str = Field(min_length=1, max_length=256)
    source_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    validity: ObservationValidity
    artifact_ids: tuple[str, ...] = ()


class TaskReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["memory", "skill", "artifact"]
    reference_id: str = Field(min_length=1, max_length=256)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    source_message_id: str = Field(min_length=1, max_length=200)
    source_call_id: str | None = Field(default=None, max_length=256)


class TaskItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=MAX_TASK_ITEM_CHARS)
    status: TaskItemStatus

    @model_validator(mode="after")
    def validate_text(self) -> TaskItem:
        if not self.text.strip():
            raise ValueError("task state item cannot be blank")
        return self


class TaskState(BaseModel):
    """Canonical work-state projection; it is not an execution authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = TASK_STATE_SCHEMA_VERSION
    revision: int = Field(default=0, ge=0)
    current_source_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    requirements: tuple[TaskRequirement, ...] = ()
    acceptance_criteria: tuple[TaskAcceptanceCriterion, ...] = ()
    observations: tuple[TaskObservation, ...] = ()
    hypotheses: tuple[TaskItem, ...] = ()
    plan: tuple[TaskItem, ...] = ()
    questions: tuple[TaskItem, ...] = ()
    blockers: tuple[TaskItem, ...] = ()
    references: tuple[TaskReference, ...] = ()

    @model_validator(mode="after")
    def validate_collections(self) -> TaskState:
        for name in ("requirements", "acceptance_criteria", "observations", "references"):
            records = getattr(self, name)
            ids = [
                getattr(record, "requirement_id", None)
                or getattr(record, "criterion_id", None)
                or getattr(record, "observation_id", None)
                or f"{record.kind}:{record.reference_id}"
                for record in records
            ]
            if len(ids) != len(set(ids)):
                raise ValueError(f"task state {name} contain duplicate identities")
        for name in ("hypotheses", "plan", "questions", "blockers"):
            ids = [item.item_id for item in getattr(self, name)]
            if len(ids) > MAX_TASK_STATE_ITEMS or len(ids) != len(set(ids)):
                raise ValueError(f"task state {name} exceed or duplicate their item limit")
        if len(self.observations) > MAX_TASK_STATE_ITEMS * 4:
            raise ValueError("task state observations exceed their item limit")
        if len(self.references) > MAX_TASK_STATE_ITEMS * 4:
            raise ValueError("task state references exceed their item limit")
        return self


class TaskStateOperation(BaseModel):
    """Closed operation shape accepted by the model-facing update tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["add", "set_status", "resolve"]
    collection: Literal["plan", "hypotheses", "questions", "blockers"]
    text: str | None = Field(default=None, max_length=MAX_TASK_ITEM_CHARS)
    item_id: str | None = Field(default=None, max_length=200)
    status: TaskItemStatus | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> TaskStateOperation:
        if self.action == "add":
            if self.text is None or not self.text.strip() or self.item_id or self.status:
                raise ValueError("add requires only non-empty text")
        elif self.action == "set_status":
            if not self.item_id or self.text is not None or self.status is None:
                raise ValueError("set_status requires item_id and status")
            if self.collection != "plan" or self.status not in {
                TaskItemStatus.TODO,
                TaskItemStatus.IN_PROGRESS,
                TaskItemStatus.DONE,
                TaskItemStatus.BLOCKED,
            }:
                raise ValueError("set_status is only supported for plan items")
        else:
            if (
                self.collection == "plan"
                or not self.item_id
                or self.text is not None
                or self.status is not None
            ):
                raise ValueError("resolve requires a note collection and item_id")
        return self


class TaskStateCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = TASK_STATE_SCHEMA_VERSION
    state: TaskState
    state_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    history_prefix_count: int = Field(ge=1)
    history_prefix_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def canonical_task_state(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TaskStateError("task state is not finite JSON data") from exc


def task_state_sha256(state: TaskState) -> str:
    return hashlib.sha256(canonical_task_state(state.model_dump(mode="json"))).hexdigest()


def _bounded(state: TaskState) -> TaskState:
    if len(canonical_task_state(state.model_dump(mode="json"))) > MAX_TASK_STATE_BYTES:
        raise TaskStateError("task state exceeds the 256 KiB persistent limit")
    return state


class TaskStateReducer:
    """Pure, all-or-nothing reducer for user, host and model operations."""

    @staticmethod
    def add_user_requirement(
        state: TaskState,
        *,
        message_id: str,
        turn: int,
        content: str,
        receipt: Mapping[str, object],
        receipt_verified: bool,
        supersedes: Sequence[str] = (),
    ) -> TaskState:
        if not receipt_verified:
            raise TaskStateError("direct user input has no verified host receipt")
        if (
            not isinstance(message_id, str)
            or not message_id
            or type(turn) is not int
            or turn < 1
            or not isinstance(content, str)
            or not content.strip()
            or len(content) > MAX_TASK_REQUIREMENT_CHARS
        ):
            raise TaskStateError("direct user requirement exceeds its bounded contract")
        expected_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if (
            receipt.get("issuer") != "tracefix-host-v1"
            or receipt.get("message_id") != message_id
            or receipt.get("turn") != turn
            or receipt.get("content_sha256") != expected_sha
        ):
            raise TaskStateError("direct user requirement does not match its host receipt")
        if any(item.message_id == message_id for item in state.requirements):
            raise TaskStateError("direct user requirement was already recorded")
        targets = tuple(supersedes)
        if len(targets) > MAX_TASK_STATE_OPERATIONS or len(targets) != len(set(targets)):
            raise TaskStateError("supersedes references exceed their bounded contract")
        active = {
            item.requirement_id: item for item in state.requirements if item.status == "active"
        }
        if any(target not in active for target in targets):
            raise TaskStateError("supersedes must reference an active direct user requirement")
        requirements = tuple(
            item.model_copy(update={"status": "superseded", "superseded_by": message_id})
            if item.requirement_id in targets
            else item
            for item in state.requirements
        )
        requirement = TaskRequirement(
            requirement_id=message_id,
            message_id=message_id,
            receipt_id=message_id,
            turn=turn,
            text=content,
        )
        updated = state.model_copy(
            update={"requirements": (*requirements, requirement), "revision": state.revision + 1}
        )
        return _bounded(updated)

    @staticmethod
    def apply_model_operations(
        state: TaskState,
        *,
        expected_revision: int,
        operations: Sequence[TaskStateOperation | Mapping[str, object]],
    ) -> tuple[TaskState, tuple[str, ...]]:
        if type(expected_revision) is not int or expected_revision != state.revision:
            raise TaskStateRevisionConflict("task state revision changed; read the latest state")
        if not isinstance(operations, Sequence) or isinstance(operations, str | bytes):
            raise TaskStateError("task state operations must be a list")
        if not 1 <= len(operations) <= MAX_TASK_STATE_OPERATIONS:
            raise TaskStateError("a task state update must contain 1..16 operations")
        try:
            parsed = tuple(
                item
                if isinstance(item, TaskStateOperation)
                else TaskStateOperation.model_validate(item)
                for item in operations
            )
        except Exception as exc:
            raise TaskStateError("task state operations are invalid") from exc

        pending = {
            name: list(getattr(state, name))
            for name in ("hypotheses", "plan", "questions", "blockers")
        }
        created: list[str] = []
        for operation in parsed:
            items = pending[operation.collection]
            if operation.action == "add":
                if len(items) >= MAX_TASK_STATE_ITEMS:
                    raise TaskStateError(f"task state {operation.collection} are full")
                item_id = f"{operation.collection}-{uuid4().hex}"
                status = (
                    TaskItemStatus.TODO if operation.collection == "plan" else TaskItemStatus.OPEN
                )
                items.append(TaskItem(item_id=item_id, text=operation.text or "", status=status))
                created.append(item_id)
                continue
            index = next(
                (i for i, item in enumerate(items) if item.item_id == operation.item_id), None
            )
            if index is None:
                raise TaskStateError("task state operation references an unknown item")
            item = items[index]
            if operation.action == "set_status":
                items[index] = item.model_copy(update={"status": operation.status})
            else:
                items[index] = item.model_copy(update={"status": TaskItemStatus.RESOLVED})

        updated = state.model_copy(
            update={
                **{name: tuple(items) for name, items in pending.items()},
                "revision": state.revision + 1,
            }
        )
        return _bounded(updated), tuple(created)

    @staticmethod
    def add_observation(
        state: TaskState,
        observation: TaskObservation,
    ) -> TaskState:
        if any(item.observation_id == observation.observation_id for item in state.observations):
            raise TaskStateError("task state observation already exists")
        observations = state.observations
        current_source = state.current_source_sha256
        if observation.source_sha256 is not None and current_source is not None:
            if observation.source_sha256 != current_source:
                observations = tuple(
                    item.model_copy(update={"validity": ObservationValidity.STALE})
                    if item.source_sha256 is not None
                    and item.source_sha256 != observation.source_sha256
                    and item.validity is ObservationValidity.VALID
                    else item
                    for item in observations
                )
        validity = (
            ObservationValidity.VALID
            if observation.source_sha256 is not None
            else ObservationValidity.HISTORICAL
        )
        observation = observation.model_copy(update={"validity": validity})
        updated = state.model_copy(
            update={
                "current_source_sha256": observation.source_sha256 or current_source,
                "observations": (*observations, observation),
                "revision": state.revision + 1,
            }
        )
        return _bounded(updated)

    @staticmethod
    def add_reference(state: TaskState, reference: TaskReference) -> TaskState:
        key = (reference.kind, reference.reference_id)
        if any((item.kind, item.reference_id) == key for item in state.references):
            return state
        updated = state.model_copy(
            update={"references": (*state.references, reference), "revision": state.revision + 1}
        )
        return _bounded(updated)


class TaskStateRenderer:
    """Deterministic bounded work summary that preserves active requirements."""

    @staticmethod
    def render(state: TaskState) -> str:
        required = {
            "schema_version": state.schema_version,
            "revision": state.revision,
            "current_source_sha256": state.current_source_sha256,
            "requirements": [
                item.model_dump(mode="json")
                for item in state.requirements
                if item.status == "active"
            ],
            "acceptance_criteria": [
                item.model_dump(mode="json") for item in state.acceptance_criteria
            ],
            "plan": [item.model_dump(mode="json") for item in state.plan],
            "hypotheses": [
                item.model_dump(mode="json")
                for item in state.hypotheses
                if item.status is not TaskItemStatus.RESOLVED
            ],
            "questions": [
                item.model_dump(mode="json")
                for item in state.questions
                if item.status is not TaskItemStatus.RESOLVED
            ],
            "blockers": [
                item.model_dump(mode="json")
                for item in state.blockers
                if item.status is not TaskItemStatus.RESOLVED
            ],
        }
        observations = [item.model_dump(mode="json") for item in state.observations]
        references = [item.model_dump(mode="json") for item in state.references]
        omitted_observations = 0
        omitted_references = 0
        while True:
            payload = {
                **required,
                "observations": observations[-16:] if observations else [],
                "references": references[-32:] if references else [],
                "omitted": {
                    "observations": omitted_observations,
                    "references": omitted_references,
                },
            }
            rendered = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if len(rendered.encode("utf-8")) <= MAX_TASK_STATE_SUMMARY_BYTES:
                return rendered
            if observations:
                observations = observations[1:]
                omitted_observations += 1
            elif references:
                references = references[1:]
                omitted_references += 1
            else:
                raise TaskStateError(
                    "active requirements and acceptance summary exceed the 8 KiB limit"
                )


class TaskStateSession:
    """Thread-safe owner for one runtime's canonical TaskState projection."""

    def __init__(self, acceptance_criteria: Sequence[TaskAcceptanceCriterion] = ()) -> None:
        self._lock = threading.RLock()
        self._acceptance_criteria = tuple(acceptance_criteria)
        self._state = TaskState(acceptance_criteria=self._acceptance_criteria)
        _bounded(self._state)

    def snapshot(self) -> TaskState:
        with self._lock:
            return TaskState.model_validate(self._state.model_dump(mode="json"))

    def reset(self) -> None:
        with self._lock:
            self._state = TaskState(acceptance_criteria=self._acceptance_criteria)

    def add_user_input(
        self,
        *,
        message_id: str,
        turn: int,
        content: str,
        receipt: Mapping[str, object],
        receipt_verified: bool,
        supersedes: Sequence[str] = (),
    ) -> TaskState:
        with self._lock:
            updated = TaskStateReducer.add_user_requirement(
                self._state,
                message_id=message_id,
                turn=turn,
                content=content,
                receipt=receipt,
                receipt_verified=receipt_verified,
                supersedes=supersedes,
            )
            TaskStateRenderer.render(updated)
            self._state = updated
            return updated

    def update(
        self,
        expected_revision: int,
        operations: Sequence[TaskStateOperation | Mapping[str, object]],
    ) -> tuple[TaskState, tuple[str, ...]]:
        with self._lock:
            updated, created = TaskStateReducer.apply_model_operations(
                self._state, expected_revision=expected_revision, operations=operations
            )
            TaskStateRenderer.render(updated)
            self._state = updated
            return updated, created

    def observe_tool_result(
        self,
        *,
        call_id: str,
        tool_name: str,
        success: bool,
        output: object,
        source_message_id: str,
        source_sha256: str | None,
    ) -> TaskState:
        if tool_name in {"get_task_state", "update_task_state"}:
            return self.snapshot()
        output_map = output if isinstance(output, Mapping) else {}
        test_status = output_map.get("test_status")
        if tool_name in {"run_tests", "run_validation"}:
            kind = "test_passed" if success and test_status == "passed" else "test_result"
            summary = f"{tool_name}: {test_status or ('success' if success else 'failed')}"
        elif tool_name == "apply_patch":
            kind = "patch_result"
            summary = "apply_patch succeeded" if success else "apply_patch failed"
        elif tool_name in {"read_file", "search_code", "get_git_diff"}:
            kind = "source_observation"
            summary = f"{tool_name} returned {'success' if success else 'failure'}"
        elif tool_name in {"read_tool_artifact", "search_tool_artifact"}:
            kind = "artifact_lookup"
            summary = f"{tool_name} returned {'success' if success else 'failure'}"
        else:
            kind = "tool_result"
            summary = f"{tool_name} returned {'success' if success else 'failure'}"
        artifact_ids = tuple(sorted(_artifact_ids(output_map)))
        artifact_hashes = _artifact_ref_hashes(output_map)
        observation = TaskObservation(
            observation_id=uuid4().hex,
            kind=kind,
            summary=summary,
            source_message_id=source_message_id,
            source_call_id=call_id,
            source_sha256=(
                source_sha256
                if kind in {"test_passed", "test_result", "patch_result", "source_observation"}
                else None
            ),
            validity=(
                ObservationValidity.VALID
                if kind in {"test_passed", "test_result", "patch_result", "source_observation"}
                and source_sha256 is not None
                else ObservationValidity.HISTORICAL
            ),
            artifact_ids=artifact_ids,
        )
        with self._lock:
            updated = TaskStateReducer.add_observation(self._state, observation)
            for artifact_id in artifact_ids:
                updated = TaskStateReducer.add_reference(
                    updated,
                    TaskReference(
                        kind="artifact",
                        reference_id=artifact_id,
                        sha256=artifact_hashes.get(artifact_id),
                        source_message_id=source_message_id,
                        source_call_id=call_id,
                    ),
                )
            TaskStateRenderer.render(updated)
            self._state = updated
            return updated

    def add_reference(self, reference: TaskReference) -> TaskState:
        with self._lock:
            updated = TaskStateReducer.add_reference(self._state, reference)
            TaskStateRenderer.render(updated)
            self._state = updated
            return updated

    def checkpoint(self, history: Sequence[object]) -> dict[str, object]:
        with self._lock:
            state = self._state
            if not history:
                raise TaskStateError("cannot checkpoint task state without committed history")
            normalized_history = [_message_payload(item) for item in history]
            value = TaskStateCheckpoint(
                state=state,
                state_sha256=task_state_sha256(state),
                history_prefix_count=len(normalized_history),
                history_prefix_sha256=hashlib.sha256(
                    canonical_task_state(normalized_history)
                ).hexdigest(),
            )
            return value.model_dump(mode="json")

    def restore(
        self,
        raw: object,
        history: Sequence[object],
        *,
        trusted_receipts: Mapping[str, Mapping[str, object]] | None = None,
        artifact_ids: set[str] | frozenset[str] | None = None,
    ) -> TaskState:
        try:
            checkpoint = TaskStateCheckpoint.model_validate(raw)
        except Exception as exc:
            raise TaskStateError("checkpoint TaskState is malformed") from exc
        normalized_history = [_message_payload(item) for item in history]
        if (
            checkpoint.state_sha256 != task_state_sha256(checkpoint.state)
            or checkpoint.history_prefix_count != len(normalized_history)
            or checkpoint.history_prefix_sha256
            != hashlib.sha256(canonical_task_state(normalized_history)).hexdigest()
        ):
            raise TaskStateError("checkpoint TaskState hash or history prefix changed")
        messages = {
            str(message.get("id")): message
            for message in normalized_history
            if isinstance(message, dict) and isinstance(message.get("id"), str)
        }
        for requirement in checkpoint.state.requirements:
            message = messages.get(requirement.message_id)
            metadata = message.get("metadata", {}) if isinstance(message, dict) else {}
            receipt = (
                metadata.get("trusted_user_input_receipt") if isinstance(metadata, dict) else None
            )
            if (
                not isinstance(message, dict)
                or message.get("role") != "user"
                or message.get("content") != requirement.text
                or not isinstance(receipt, dict)
                or receipt.get("issuer") != "tracefix-host-v1"
                or receipt.get("message_id") != requirement.receipt_id
                or receipt.get("turn") != requirement.turn
                or receipt.get("content_sha256")
                != hashlib.sha256(requirement.text.encode("utf-8")).hexdigest()
                or (
                    trusted_receipts is not None
                    and trusted_receipts.get(requirement.receipt_id) != receipt
                )
            ):
                raise TaskStateError("checkpoint requirement has no committed direct-user receipt")
        for observation in checkpoint.state.observations:
            message = messages.get(observation.source_message_id)
            metadata = message.get("metadata", {}) if isinstance(message, dict) else {}
            tool_name = metadata.get("tool_name") if isinstance(metadata, dict) else None
            if (
                not isinstance(message, dict)
                or message.get("role") != "tool"
                or message.get("tool_call_id") != observation.source_call_id
                or not _tool_kind_matches(observation.kind, tool_name)
            ):
                raise TaskStateError("checkpoint observation references an uncommitted tool result")
        for reference in checkpoint.state.references:
            message = messages.get(reference.source_message_id)
            if not isinstance(message, dict):
                raise TaskStateError("checkpoint reference is outside the committed history")
            metadata = message.get("metadata", {})
            if reference.kind == "memory" and (
                message.get("role") != "user"
                or metadata.get("kind") != "memory_recall_bundle"
                or metadata.get("bundle_sha256") != reference.sha256
                or reference.reference_id != reference.sha256
            ):
                raise TaskStateError("checkpoint memory reference is not in the committed history")
            if reference.kind == "skill" and (
                message.get("role") != "user"
                or metadata.get("kind") != "skill_instructions"
                or metadata.get("content_sha256") != reference.sha256
                or metadata.get("skill_name") != reference.reference_id
            ):
                raise TaskStateError("checkpoint skill reference is not in the committed history")
            if reference.kind == "artifact" and (
                message.get("role") != "tool"
                or message.get("tool_call_id") != reference.source_call_id
                or reference.reference_id not in _artifact_ids_from_message(message)
                or reference.sha256 is None
                or _artifact_ref_hashes_from_message(message).get(reference.reference_id)
                != reference.sha256
                or artifact_ids is None
                or reference.reference_id not in artifact_ids
            ):
                raise TaskStateError("checkpoint artifact reference is not in the committed run")
        if self._acceptance_criteria != checkpoint.state.acceptance_criteria:
            raise TaskStateError("checkpoint acceptance criteria differ from the run configuration")
        _bounded(checkpoint.state)
        TaskStateRenderer.render(checkpoint.state)
        with self._lock:
            self._state = checkpoint.state
            return self.snapshot()


def _message_payload(message: object) -> dict[str, Any]:
    if isinstance(message, BaseModel):
        return message.model_dump(mode="json")
    if isinstance(message, dict):
        return message
    raise TaskStateError("history contains an invalid message reference")


def _artifact_ids(value: object, *, depth: int = 0) -> set[str]:
    if depth > 8:
        return set()
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (
                key == "artifact_id"
                and isinstance(item, str)
                and re.fullmatch(r"[a-f0-9]{32}", item)
            ):
                found.add(item)
            else:
                found.update(_artifact_ids(item, depth=depth + 1))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value[:256]:
            found.update(_artifact_ids(item, depth=depth + 1))
    return found


def _artifact_ref_hashes(value: object, *, depth: int = 0) -> dict[str, str]:
    if depth > 8:
        return {}
    found: dict[str, str] = {}
    if isinstance(value, Mapping):
        artifact_id = value.get("artifact_id")
        view_sha256 = value.get("view_sha256")
        if (
            isinstance(artifact_id, str)
            and re.fullmatch(r"[a-f0-9]{32}", artifact_id)
            and isinstance(view_sha256, str)
            and _SHA256.fullmatch(view_sha256)
        ):
            found[artifact_id] = view_sha256
        for item in value.values():
            found.update(_artifact_ref_hashes(item, depth=depth + 1))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value[:256]:
            found.update(_artifact_ref_hashes(item, depth=depth + 1))
    return found


def _artifact_ids_from_message(message: Mapping[str, Any]) -> set[str]:
    try:
        content = json.loads(message.get("content") or "")
    except (json.JSONDecodeError, TypeError):
        return set()
    return _artifact_ids(content)


def _artifact_ref_hashes_from_message(message: Mapping[str, Any]) -> dict[str, str]:
    try:
        content = json.loads(message.get("content") or "")
    except (json.JSONDecodeError, TypeError):
        return {}
    return _artifact_ref_hashes(content)


def _tool_kind_matches(kind: str, tool_name: object) -> bool:
    allowed = {
        "test_passed": {"run_tests", "run_validation"},
        "test_result": {"run_tests", "run_validation"},
        "patch_result": {"apply_patch"},
        "source_observation": {"read_file", "search_code", "get_git_diff"},
        "artifact_lookup": {"read_tool_artifact", "search_tool_artifact"},
    }
    if kind == "tool_result":
        return isinstance(tool_name, str) and bool(tool_name)
    return isinstance(tool_name, str) and tool_name in allowed.get(kind, set())
