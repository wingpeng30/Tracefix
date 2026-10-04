"""Continuous rounds keep real workspaces and resource accounting across requests."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from examples.replay_ordinary import PATCH, ReplayClient, _git
from tracefix.agent import AgentStatus
from tracefix.checkpoint import CheckpointError, CheckpointStore, ProcessLock
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.runtime import RunConfig, TraceFixRunner


def repository(root: Path) -> Path:
    repo = root / "source"
    repo.mkdir()
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (repo / "widget.py").write_text("def next_page(page):\n    return page\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests/test_widget.py").write_text(
        "from widget import next_page\ndef test_page():\n    assert next_page(1) == 2\n",
        encoding="utf-8",
    )
    _git(repo, "init")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "base")
    return repo


def runner(sequence=None):
    client = ReplayClient()
    client.completion_cost = lambda **_kwargs: 0.0
    if sequence is not None:
        client.sequence = sequence
    return TraceFixRunner(lambda config: LiteLLMAdapter(config, client=client))


def test_three_real_rounds_correction_archives_and_cumulative_budget(tmp_path):
    repo = repository(tmp_path)
    result = runner().run(RunConfig(
        repo=repo, output_dir=tmp_path / "runs", task="Increment a page",
        model_name="offline/dialogue", env_file=None,
        test_target="tests/test_widget.py", conversation_enabled=True,
    ))
    root = Path(result.result_path).parent
    assert result.status is AgentStatus.COMPLETED
    assert result.session_status == "awaiting_user"
    inspection = TraceFixRunner.inspect(root)
    assert not inspection["resumable"] and inspection["continuable"]
    for number, message, before, after in (
        (2, "Correction: preserve negative sentinel pages", "return page + 1",
         "return page if page < 0 else page + 1"),
        (3, "Also preserve zero as a sentinel", "return page if page < 0 else page + 1",
         "return page if page <= 0 else page + 1"),
    ):
        patch = PATCH.replace("-    return page", f"-    {before}").replace(
            "+    return page + 1", f"+    {after}",
        )
        prior = result.step_count
        result = runner([
            ("read_file", {"path": "widget.py"}),
            ("apply_patch", {"patch": patch}),
            ("run_tests", {"command": "pytest -q tests/test_widget.py"}),
            ("get_git_diff", {"context_lines": 3}),
        ]).continue_turn(root, message)
        assert result.status is AgentStatus.COMPLETED
        assert result.turn_number == number and result.step_count > prior
        assert result.session_status == "awaiting_user"
        assert len(result.turn_records) == number
    assert "<= 0" in (root / "workspace/widget.py").read_text(encoding="utf-8")
    assert (repo / "widget.py").read_text(encoding="utf-8").endswith("return page\n")
    assert len(list((root / "turns").glob("*/latest.json"))) == 3
    from tracefix.report import render_report

    report = render_report(root)
    assert "当前轮次" in report.read_text(encoding="utf-8")
    with pytest.raises(CheckpointError, match="empty"):
        runner().continue_turn(root, "  ")


def test_legacy_single_task_does_not_become_a_session(tmp_path):
    result = runner().run(RunConfig(
        repo=repository(tmp_path), output_dir=tmp_path / "runs", task="Increment page",
        model_name="offline/dialogue", env_file=None,
        test_target="tests/test_widget.py",
    ))
    with pytest.raises(Exception, match="cannot be continued"):
        runner().continue_turn(Path(result.result_path).parent, "new requirement")


def test_safe_pause_can_append_a_turn_and_concurrent_writes_are_rejected(tmp_path, monkeypatch):
    original_save = CheckpointStore.save

    def pause(self, payload, *, sequence, pending_calls=()):
        value = original_save(self, payload, sequence=sequence, pending_calls=pending_calls)
        if sequence == 3:
            raise KeyboardInterrupt
        return value

    monkeypatch.setattr(CheckpointStore, "save", pause)
    result = runner().run(RunConfig(
        repo=repository(tmp_path), output_dir=tmp_path / "runs", task="Increment page",
        model_name="offline/dialogue", env_file=None,
        test_target="tests/test_widget.py", conversation_enabled=True,
    ))
    root = Path(result.result_path).parent
    assert result.status is AgentStatus.INTERRUPTED and result.session_status == "paused"
    assert TraceFixRunner.inspect(root)["continuable"]
    with ProcessLock(root), pytest.raises(CheckpointError, match="lock"):
        runner().continue_turn(root, "Finish the change")
    monkeypatch.setattr(CheckpointStore, "save", original_save)
    completed = runner([
        ("apply_patch", {"patch": PATCH}),
        ("run_tests", {"command": "pytest -q tests/test_widget.py"}),
        ("get_git_diff", {"context_lines": 3}),
    ]).continue_turn(root, "Finish the change")
    assert completed.status is AgentStatus.COMPLETED and completed.turn_number == 2


def test_round_corruption_pending_outcome_and_budget_fail_closed(tmp_path):
    result = runner().run(RunConfig(
        repo=repository(tmp_path), output_dir=tmp_path / "runs", task="Increment page",
        model_name="offline/dialogue", env_file=None,
        test_target="tests/test_widget.py", conversation_enabled=True,
    ))
    root = Path(result.result_path).parent
    pointer = root / "turns/0001/latest.json"
    raw = pointer.read_bytes()
    record = json.loads(raw)
    patch = pointer.parent / record["attempt"] / "patch.diff"
    saved = patch.read_bytes()
    patch.write_bytes(b"corrupt")
    assert not TraceFixRunner.inspect(root)["continuable"]
    patch.write_bytes(saved)
    record["attempt"] = "../outside"
    pointer.write_text(json.dumps(record), encoding="utf-8")
    assert not TraceFixRunner.inspect(root)["continuable"]
    pointer.write_bytes(raw)
    trajectory = root / "trajectory.jsonl"
    trace = trajectory.read_bytes()
    trajectory.write_bytes(trace + b'{"event_type":"model_requested"}\n')
    assert not TraceFixRunner.inspect(root)["continuable"]
    trajectory.write_bytes(trace)
    manifest = json.loads((root / "session.json").read_text(encoding="utf-8"))
    store = CheckpointStore(root, manifest["identity"])
    snapshot = store.load()
    snapshot.payload["agent"]["state"]["step_count"] = result.agent_config.max_steps
    store.save(snapshot.payload, sequence=snapshot.sequence + 1)
    assert "cumulative session budget exhausted" in TraceFixRunner.inspect(root)["continue_reasons"]
    with pytest.raises(CheckpointError, match="cannot be continued"):
        runner().continue_turn(root, "Do more without adding budget")


def test_interactive_cli_process_reentry_and_per_turn_memory(tmp_path):
    from tracefix.dialogue_replay import run_dialogue_replay

    summary = run_dialogue_replay(tmp_path / "cli")
    assert summary["accepted"] and summary["provider_calls"] == 0
    assert len(summary["sha256"]) == 3


def test_recorded_worker_uses_real_cli_and_memory_in_process(tmp_path, monkeypatch):
    from tracefix.dialogue_replay import prepare_dialogue, worker

    output = tmp_path / "worker"
    prepare_dialogue(output)
    for turn, messages in ((1, [":exit"]), (2, ["Correct negative sentinel", ":exit"]), (3, [])):
        inputs = iter(messages)
        monkeypatch.setattr("builtins.input", lambda _prompt, inputs=inputs: next(inputs))
        assert worker(output, turn) == 0
    root = next((output / "runs").glob("*/session.json")).parent
    assert TraceFixRunner.inspect(root)["turn_number"] == 3


def test_dialogue_process_receipts_allow_os_pid_reuse():
    from tracefix.dialogue_replay import _valid_process_receipts

    receipts = [
        {"turn": turn, "pid": 1234, "exit_code": 0, "provider_calls": 0}
        for turn in (1, 2, 3)
    ]
    assert _valid_process_receipts(receipts)
    receipts[2]["turn"] = 2
    assert not _valid_process_receipts(receipts)


def test_chat_loop_blank_resume_exit_and_legacy_rejection(tmp_path, monkeypatch):
    from tracefix import cli

    (tmp_path / "session.json").write_text(json.dumps({
        "schema_version": 2, "config": {"conversation_enabled": True},
    }), encoding="utf-8")
    calls = []
    fake = SimpleNamespace(
        continue_turn=lambda root, message: calls.append(("continue", root, message)),
        resume=lambda root: calls.append(("resume", root)),
    )
    monkeypatch.setattr(cli, "_print_run_result", lambda result: None)
    inputs = iter([" ", "new message", ":resume", ":quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    assert cli._chat_loop(fake, tmp_path) == 0
    assert [item[0] for item in calls] == ["continue", "resume"]
    (tmp_path / "session.json").write_text('{"schema_version":1}', encoding="utf-8")
    with pytest.raises(ValueError, match="连续会话"):
        cli._chat_loop(fake, tmp_path)
