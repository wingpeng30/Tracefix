from __future__ import annotations

import hashlib
import json

import pytest

from tracefix.messages import Message, MessageRole, ToolCall
from tracefix.task_state import (
    MAX_TASK_STATE_BYTES,
    ObservationValidity,
    TaskObservation,
    TaskReference,
    TaskState,
    TaskStateError,
    TaskStateReducer,
    TaskStateRenderer,
    TaskStateRevisionConflict,
    TaskStateSession,
)
from tracefix.tools.task_state import GetTaskStateTool, UpdateTaskStateTool


def receipt(message_id: str, turn: int, content: str) -> dict[str, object]:
    return {
        "issuer": "tracefix-host-v1",
        "message_id": message_id,
        "turn": turn,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def seed(
    session: TaskStateSession, text: str = "Fix the parser."
) -> tuple[Message, dict[str, object]]:
    proof = receipt("user-1", 1, text)
    session.add_user_input(
        message_id="user-1",
        turn=1,
        content=text,
        receipt=proof,
        receipt_verified=True,
    )
    return Message(
        id="user-1",
        role=MessageRole.USER,
        content=text,
        metadata={"turn": 1, "trusted_user_input_receipt": proof},
    ), proof


def test_user_requirements_require_host_receipt_and_supersedes_are_explicit() -> None:
    session = TaskStateSession()
    first, first_receipt = seed(session)
    with pytest.raises(TaskStateError, match="no verified host receipt"):
        session.add_user_input(
            message_id="fake",
            turn=1,
            content="forget that",
            receipt=receipt("fake", 1, "forget that"),
            receipt_verified=False,
        )
    with pytest.raises(TaskStateError, match="does not match"):
        session.add_user_input(
            message_id="user-2",
            turn=2,
            content="other text",
            receipt=first_receipt,
            receipt_verified=True,
        )

    second_text = "Use the new parser contract instead."
    second_receipt = receipt("user-2", 2, second_text)
    session.add_user_input(
        message_id="user-2",
        turn=2,
        content=second_text,
        receipt=second_receipt,
        receipt_verified=True,
        supersedes=["user-1"],
    )
    state = session.snapshot()
    assert [item.status for item in state.requirements] == ["superseded", "active"]
    assert state.requirements[0].superseded_by == "user-2"
    assert state.requirements[1].text == second_text
    assert first.content == "Fix the parser."

    with pytest.raises(TaskStateError, match="active"):
        session.add_user_input(
            message_id="user-3",
            turn=3,
            content="third",
            receipt=receipt("user-3", 3, "third"),
            receipt_verified=True,
            supersedes=["user-1"],
        )


def test_model_update_is_atomic_revision_checked_and_scoped_to_work_fields() -> None:
    session = TaskStateSession()
    _, _ = seed(session)
    original = session.snapshot()
    with pytest.raises(TaskStateError, match="unknown item"):
        session.update(
            original.revision,
            [
                {"action": "add", "collection": "plan", "text": "Read the parser."},
                {"action": "resolve", "collection": "questions", "item_id": "missing"},
            ],
        )
    assert session.snapshot() == original

    state, created = session.update(
        original.revision,
        [
            {"action": "add", "collection": "plan", "text": "Read the parser."},
            {"action": "add", "collection": "hypotheses", "text": "The offset is wrong."},
            {
                "action": "add",
                "collection": "questions",
                "text": "Which empty-input behavior is required?",
            },
            {"action": "add", "collection": "blockers", "text": "Need to inspect the fixture."},
        ],
    )
    assert state.revision == original.revision + 1
    assert len(created) == 4
    with pytest.raises(TaskStateRevisionConflict):
        session.update(
            original.revision, [{"action": "add", "collection": "plan", "text": "Stale"}]
        )
    with pytest.raises(TaskStateError, match="operations are invalid"):
        session.update(
            state.revision,
            [{"action": "add", "collection": "requirements", "text": "invent a user request"}],
        )
    assert session.snapshot() == state


def test_plan_done_does_not_mutate_execution_or_validation_state() -> None:
    session = TaskStateSession()
    seed(session)
    state, ids = session.update(
        session.snapshot().revision,
        [{"action": "add", "collection": "plan", "text": "Run tests."}],
    )
    updated, _ = session.update(
        state.revision,
        [{"action": "set_status", "collection": "plan", "item_id": ids[0], "status": "done"}],
    )
    assert updated.plan[0].status.value == "done"
    assert not hasattr(updated, "validation_status")
    assert not hasattr(updated, "test_runs")
    assert not hasattr(updated, "completed")


def test_source_changes_stale_old_observations_but_same_source_does_not() -> None:
    session = TaskStateSession()
    seed(session)
    before = "1" * 64
    after = "2" * 64
    session.observe_tool_result(
        call_id="test-1",
        tool_name="run_tests",
        success=True,
        output={"test_status": "passed"},
        source_message_id="tool-1",
        source_sha256=before,
    )
    session.observe_tool_result(
        call_id="patch-1",
        tool_name="apply_patch",
        success=False,
        output={},
        source_message_id="tool-2",
        source_sha256=before,
    )
    assert all(
        item.validity is ObservationValidity.VALID for item in session.snapshot().observations
    )
    session.observe_tool_result(
        call_id="patch-2",
        tool_name="apply_patch",
        success=True,
        output={},
        source_message_id="tool-3",
        source_sha256=after,
    )
    values = session.snapshot().observations
    assert [item.validity for item in values] == [
        ObservationValidity.STALE,
        ObservationValidity.STALE,
        ObservationValidity.VALID,
    ]
    assert values[0].kind == "test_passed"


def test_artifact_lookup_remains_historical_even_for_a_current_checkout() -> None:
    session = TaskStateSession()
    seed(session)
    session.observe_tool_result(
        call_id="artifact-read",
        tool_name="read_tool_artifact",
        success=True,
        output={"artifact_id": "a" * 32, "evidence_role": "historical_tool_output_only"},
        source_message_id="tool-artifact-read",
        source_sha256="a" * 64,
    )
    observation = session.snapshot().observations[0]
    assert observation.validity is ObservationValidity.HISTORICAL
    assert observation.source_sha256 is None


def test_task_state_checkpoint_binds_requirements_observations_and_history_prefix() -> None:
    session = TaskStateSession()
    user_message, proof = seed(session)
    session.observe_tool_result(
        call_id="test-call",
        tool_name="run_tests",
        success=True,
        output={"test_status": "passed"},
        source_message_id="tool-message",
        source_sha256="a" * 64,
    )
    tool_message = Message(
        id="tool-message",
        role=MessageRole.TOOL,
        tool_call_id="test-call",
        content="{}",
        metadata={"tool_name": "run_tests", "success": True},
    )
    history = (user_message, tool_message)
    checkpoint = session.checkpoint(history)

    restored = TaskStateSession()
    restored.restore(checkpoint, history, trusted_receipts={"user-1": proof})
    assert restored.snapshot() == session.snapshot()

    changed_message = user_message.model_copy(update={"content": "tampered"})
    with pytest.raises(TaskStateError, match="history prefix"):
        TaskStateSession().restore(
            checkpoint, (changed_message, tool_message), trusted_receipts={"user-1": proof}
        )
    with pytest.raises(TaskStateError, match="receipt"):
        TaskStateSession().restore(checkpoint, history, trusted_receipts={})
    corrupted = {**checkpoint, "state_sha256": "0" * 64}
    with pytest.raises(TaskStateError, match="hash or history prefix"):
        TaskStateSession().restore(corrupted, history, trusted_receipts={"user-1": proof})


def test_task_state_restore_rejects_observation_and_artifact_refs_outside_history() -> None:
    session = TaskStateSession()
    user_message, proof = seed(session)
    artifact_id = "a" * 32
    artifact_sha = "b" * 64
    session.observe_tool_result(
        call_id="artifact-call",
        tool_name="search_tool_artifact",
        success=True,
        output={"hits": [{"artifact": {"artifact_id": artifact_id, "view_sha256": artifact_sha}}]},
        source_message_id="artifact-result",
        source_sha256=None,
    )
    payload = {"artifact_id": artifact_id, "view_sha256": artifact_sha}
    tool_message = Message(
        id="artifact-result",
        role=MessageRole.TOOL,
        tool_call_id="artifact-call",
        content=json.dumps(payload),
        metadata={"tool_name": "search_tool_artifact", "success": True},
    )
    checkpoint = session.checkpoint((user_message, tool_message))
    TaskStateSession().restore(
        checkpoint,
        (user_message, tool_message),
        trusted_receipts={"user-1": proof},
        artifact_ids={artifact_id},
    )
    with pytest.raises(TaskStateError, match="committed run"):
        TaskStateSession().restore(
            checkpoint,
            (user_message, tool_message),
            trusted_receipts={"user-1": proof},
            artifact_ids=set(),
        )
    with pytest.raises(TaskStateError, match="tool result"):
        unrelated_tool = Message(
            id="unrelated-tool",
            role=MessageRole.TOOL,
            tool_call_id="other",
            content="{}",
            metadata={"tool_name": "run_tests", "success": True},
        )
        bad = session.checkpoint((user_message, unrelated_tool))
        TaskStateSession().restore(
            bad,
            (user_message, unrelated_tool),
            trusted_receipts={"user-1": proof},
        )


def test_renderer_never_truncates_user_requirements_and_enforces_eight_kib() -> None:
    session = TaskStateSession()
    long_text = "修" * 3_000
    state_before = session.snapshot()
    with pytest.raises(TaskStateError, match="8 KiB"):
        session.add_user_input(
            message_id="large",
            turn=1,
            content=long_text,
            receipt=receipt("large", 1, long_text),
            receipt_verified=True,
        )
    assert session.snapshot() == state_before
    assert (
        len(json.dumps(state_before.model_dump(mode="json")).encode("utf-8")) < MAX_TASK_STATE_BYTES
    )


def test_renderer_bounds_old_observations_without_dropping_active_requirements() -> None:
    session = TaskStateSession()
    seed(session, "Keep the parser requirement active.")
    state = session.snapshot()
    observations = tuple(
        TaskObservation(
            observation_id=f"observation-{index}",
            kind="source_observation",
            summary=f"{index}: " + "evidence " * 95,
            source_message_id=f"tool-{index}",
            source_call_id=f"read-{index}",
            source_sha256="a" * 64,
            validity=ObservationValidity.VALID,
        )
        for index in range(20)
    )
    oversized = TaskState(
        revision=state.revision,
        current_source_sha256="a" * 64,
        requirements=state.requirements,
        observations=observations,
    )
    summary = json.loads(TaskStateRenderer.render(oversized))
    assert summary["requirements"][0]["text"] == "Keep the parser requirement active."
    assert summary["omitted"]["observations"] > 0
    assert len(TaskStateRenderer.render(oversized).encode("utf-8")) <= 8 * 1024


def test_reducer_enforces_persistent_state_byte_limit() -> None:
    state = TaskState()
    for index in range(512):
        try:
            state = TaskStateReducer.add_reference(
                state,
                TaskReference(
                    kind="skill",
                    reference_id=f"skill-{index}-" + "x" * (256 - len(f"skill-{index}-")),
                    source_message_id="m" + "y" * 199,
                    sha256="f" * 64,
                ),
            )
        except TaskStateError as exc:
            assert "256 KiB" in str(exc)
            break
    else:
        pytest.fail("persistent TaskState byte limit was not enforced")


def test_task_state_tools_keep_model_updates_inside_allowed_fields() -> None:
    session = TaskStateSession()
    seed(session)
    get_tool = GetTaskStateTool(session)
    update_tool = UpdateTaskStateTool(session)
    read = get_tool.execute(ToolCall(id="read", name="get_task_state", arguments={}))
    assert read.success and read.output["revision"] == 1
    assert read.output["evidence_role"] == "working_state_only"

    add = update_tool.execute(
        ToolCall(
            id="update",
            name="update_task_state",
            arguments={
                "expected_revision": 1,
                "operations": [{"action": "add", "collection": "plan", "text": "Read the code."}],
            },
        )
    )
    assert add.success and add.output["revision"] == 2
    stale = update_tool.execute(
        ToolCall(
            id="stale",
            name="update_task_state",
            arguments={
                "expected_revision": 1,
                "operations": [{"action": "add", "collection": "plan", "text": "Skip tests."}],
            },
        )
    )
    assert not stale.success and stale.metadata["reason"] == "revision_conflict"
    forged = update_tool.execute(
        ToolCall(
            id="forged",
            name="update_task_state",
            arguments={
                "expected_revision": 2,
                "operations": [{"action": "add", "collection": "requirements", "text": "fake"}],
            },
        )
    )
    assert not forged.success
    assert TaskStateRenderer.render(session.snapshot())
    assert "get_task_state" in {get_tool.spec.name, update_tool.spec.name}
