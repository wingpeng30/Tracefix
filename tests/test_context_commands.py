"""Context inspection and safe manual compaction APIs."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from tracefix import AgentConfig, ContextConfig, ContextManager, ContextSessionState, Message
from tracefix.checkpoint import CheckpointError, CheckpointStore, ProcessLock
from tracefix.messages import MessageRole, ToolCall
from tracefix.runtime import RunConfig, TraceFixRunner
from tracefix.tools import ToolResult, ToolSpec


def _history(count: int = 8) -> tuple[Message, ...]:
    messages = [Message(role=MessageRole.SYSTEM, content="system prompt")]
    messages.append(Message(role=MessageRole.USER, content="fix the parser"))
    for number in range(count):
        call_id = f"read-{number}"
        call = ToolCall(id=call_id, name="read_file", arguments={"path": f"src/{number}.py"})
        messages.append(Message(role=MessageRole.ASSISTANT, content="read", tool_calls=(call,)))
        result = ToolResult(
            call_id=call_id,
            tool_name="read_file",
            success=True,
            output={"path": f"src/{number}.py", "content": "evidence " * 180},
        )
        messages.append(
            Message(role=MessageRole.TOOL, content=result.model_dump_json(), tool_call_id=call_id)
        )
    return tuple(messages)


def _saved_run(root, monkeypatch) -> tuple[RunConfig, CheckpointStore]:
    context = ContextConfig(
        context_window_tokens=10_000,
        compaction_trigger_tokens=5_000,
        retain_ratio=0.2,
        output_reserve_tokens=100,
    )
    config = RunConfig(
        repo=root,
        task="fix",
        output_dir=root / "runs",
        per_request_output_tokens=100,
        agent_config=AgentConfig(context=context),
    )
    tools = (
        ToolSpec(name="read_file", description="read"),
        ToolSpec(name="compact_context", description="compact"),
    )
    history = _history()
    state = ContextSessionState()
    ContextManager(context).prepare(history, tools, state=state)
    agent_payload = {
        "task_id": "context-test",
        "active_seconds": 0,
        "history": [message.model_dump(mode="json") for message in history],
        "memory": {"_context_session_state": state.model_dump(mode="json")},
        "state": {"context_metrics": {}, "step_count": 2, "status": "interrupted"},
    }
    trace = root / "trajectory.jsonl"
    trace.write_bytes(b"")
    identity = {"run_id": root.name, "context-test": True}
    manifest = {"schema_version": 1, "identity": identity, "config": config.model_dump(mode="json")}
    (root / "session.json").write_text(json.dumps(manifest), encoding="utf-8")
    store = CheckpointStore(root, identity)
    store.save(
        {"agent": agent_payload, "trace_size": 0, "trace_sha256": hashlib.sha256(b"").hexdigest()},
        sequence=1,
    )

    def safe_inspect(_cls, _run_dir, **_kwargs):
        return {"resumable": True, "continuable": False, "reasons": []}

    monkeypatch.setattr(TraceFixRunner, "inspect", classmethod(safe_inspect))
    return config, store


def test_context_inspect_and_compact_persist_projection_with_trace_identity(
    tmp_path, monkeypatch
) -> None:
    _config, store = _saved_run(tmp_path, monkeypatch)
    runner = TraceFixRunner()

    before = runner.inspect_context(tmp_path)
    result = runner.compact_context(tmp_path)
    after = runner.inspect_context(tmp_path)

    assert before["projection_epoch"] == 0
    assert result["compacted"] is True
    assert after["projection_epoch"] == 1
    assert after["projection_sha256"] != before["projection_sha256"]
    assert store.load().sequence == 1  # context metadata does not invent a Docker tool batch
    assert store.inspect().resumable
    events = [json.loads(line) for line in (tmp_path / "trajectory.jsonl").read_text().splitlines()]
    assert [event["event_type"] for event in events] == [
        "context_prepared",
        "context_compacted",
    ]


def test_context_inspect_uses_and_validates_the_saved_tokenizer_identity(
    tmp_path, monkeypatch
) -> None:
    _config, store = _saved_run(tmp_path, monkeypatch)
    monkeypatch.setattr(
        TraceFixRunner,
        "_saved_context_counter",
        staticmethod(lambda _model: lambda _messages, _tools: SimpleNamespace(
            tokens=321, status="estimate", method="official-v41", identity="token-sha"
        )),
    )
    inspection = TraceFixRunner().inspect_context(tmp_path)
    assert inspection["estimated_tokens"] == 321
    assert inspection["count_method"] == "official-v41"
    assert inspection["tokenizer_identity"] == "token-sha"

    snapshot = store.load()
    snapshot.payload["agent"]["memory"]["_context_session_state"][
        "tokenizer_identity"
    ] = "saved-sha"
    store.update_payload(snapshot.payload, expected_sequence=snapshot.sequence)
    with pytest.raises(CheckpointError, match="differs from checkpoint"):
        TraceFixRunner().inspect_context(tmp_path)


def test_context_compact_refuses_running_sessions_and_honors_process_lock(
    tmp_path, monkeypatch
) -> None:
    _config, _store = _saved_run(tmp_path, monkeypatch)
    monkeypatch.setattr(
        TraceFixRunner,
        "inspect",
        classmethod(
            lambda _cls, _run_dir, **_kwargs: {
                "resumable": False,
                "continuable": False,
                "reasons": ["running"],
            }
        ),
    )
    with pytest.raises(CheckpointError, match="safe paused session"):
        TraceFixRunner().compact_context(tmp_path)

    monkeypatch.setattr(
        TraceFixRunner,
        "inspect",
        classmethod(
            lambda _cls, _run_dir, **_kwargs: {
                "resumable": True,
                "continuable": False,
                "reasons": [],
            }
        ),
    )
    lock = ProcessLock(tmp_path).acquire()
    try:
        with pytest.raises(CheckpointError):
            TraceFixRunner().compact_context(tmp_path)
    finally:
        lock.release()


def test_context_inspect_rejects_corrupt_checkpoint(tmp_path) -> None:
    root = tmp_path
    config = RunConfig(repo=root, task="fix", output_dir=root / "runs")
    (root / "session.json").write_text(
        json.dumps(
            {
                "identity": {"run_id": "corrupt"},
                "config": config.model_dump(mode="json"),
            }
        ),
        encoding="utf-8",
    )
    (root / "checkpoint.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(CheckpointError, match="not readable"):
        TraceFixRunner.inspect_context(root)
