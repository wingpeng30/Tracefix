"""Real local checkout recovery through recorded LiteLLM responses."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from examples.replay_ordinary import ReplayClient
from tracefix.checkpoint import CheckpointStore
from tracefix.messages import ToolCall
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.runtime import RunConfig, TraceFixRunner
from tracefix.tools.builtin import RunTestsTool


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_resume_after_completed_tool_batch(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (source / "widget.py").write_text("def next_page(page):\n    return page\n", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "test_widget.py").write_text(
        "from widget import next_page\n\ndef test_next_page():\n    assert next_page(1) == 2\n",
        encoding="utf-8",
    )
    _git(source, "init", "-q")
    _git(source, "add", "--all")
    _git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "fixture")
    class RecordingClient(ReplayClient):
        def __init__(self) -> None:
            super().__init__()
            self.requests: list[dict] = []

        def completion(self, **kwargs):
            self.requests.append(kwargs)
            return super().completion(**kwargs)

    client = RecordingClient()
    runner = TraceFixRunner(llm_factory=lambda config: LiteLLMAdapter(config, client=client))
    original_save = CheckpointStore.save
    interrupted = False

    def interrupt_after_save(self, payload, *, sequence, pending_calls=()):
        nonlocal interrupted
        saved = original_save(self, payload, sequence=sequence, pending_calls=pending_calls)
        if sequence == 3 and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return saved

    monkeypatch.setattr(CheckpointStore, "save", interrupt_after_save)
    first = runner.run(RunConfig(
        repo=source, task="修复分页测试", model_name="offline/replay",
        output_dir=tmp_path / "runs", env_file=None,
        test_python_executable=Path(sys.executable),
        test_target="tests/test_widget.py", source_import="widget",
    ))
    assert first.status.value == "interrupted"
    run_dir = Path(first.result_path).parent
    assert TraceFixRunner.inspect(run_dir)["resumable"] is True
    checkout_file = run_dir / "workspace" / "widget.py"
    original = checkout_file.read_bytes()
    checkout_file.write_bytes(original + b"\n# changed externally\n")
    assert any(
        "checkout patch changed" in reason
        for reason in TraceFixRunner.inspect(run_dir)["reasons"]
    )
    checkout_file.write_bytes(original)
    trace_path = run_dir / "trajectory.jsonl"
    trace = trace_path.read_bytes()
    trace_path.write_bytes(trace + b'{"event_type":"model_requested"}\n')
    assert any(
        "uncommitted model or tool outcome" in reason
        for reason in TraceFixRunner.inspect(run_dir)["reasons"]
    )
    trace_path.write_bytes(trace)
    session_path = run_dir / "session.json"
    session = session_path.read_bytes()
    altered = json.loads(session)
    altered["config"]["model_name"] = "offline/changed"
    session_path.write_text(json.dumps(altered), encoding="utf-8")
    assert "identity_mismatch" in TraceFixRunner.inspect(run_dir)["reasons"]
    session_path.write_bytes(session)
    second = runner.resume(run_dir)
    assert second.status.value == "completed"
    assert second.agent_validation_status == "verified", second.model_dump(mode="json")
    assert second.step_count == 6
    assert second.test_runs == 2
    assert client.calls == 6
    assert any(
        message.get("role") == "system"
        and "TraceFix 已核验任务事实" in str(message.get("content"))
        and '"valid": true' in str(message.get("content"))
        for message in client.requests[-1]["messages"]
    )
    assert "return page + 1" in Path(second.diff_path).read_text(encoding="utf-8")
    assert subprocess.run(["git", "status", "--porcelain"], cwd=source,
                          capture_output=True, text=True, check=True).stdout == ""
    assert TraceFixRunner.inspect(run_dir)["resumable"] is False


def test_passing_pytest_that_modifies_source_is_not_verified(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "test_mutate.py").write_text(
        "from pathlib import Path\n\n"
        "def test_changes_source():\n"
        "    Path('module.py').write_text('VALUE = 2\\n')\n"
        "    assert True\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "--all")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "fixture")
    tool = RunTestsTool(
        repo, python_executable=sys.executable, evidence_dir=tmp_path / "evidence",
    )
    result = tool.execute(ToolCall(
        id="mutate-test", name="run_tests", arguments={"command": "pytest -q test_mutate.py"}
    ))
    assert result.success is False
    assert result.output["returncode"] == 0
    assert result.output["test_status"] == "invalid_test_run"
    assert result.output["source_sha256_before"] != result.output["source_sha256_after"]
