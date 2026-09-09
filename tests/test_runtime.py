import json
import os
import subprocess
from pathlib import Path

import pytest

from tracefix import (
    AgentConfig,
    AgentStatus,
    BaseLLM,
    LLMConfig,
    LLMProviderError,
    LLMResponse,
    Message,
    MessageRole,
    RunConfig,
    RunTestsTool,
    TokenUsage,
    ToolCall,
    TraceFixRunner,
    WorkspaceError,
)


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n", encoding="utf-8")
    (repo / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "--all")
    _git(
        repo,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "initial",
    )
    return repo


def _response(
    content: str | None,
    *,
    tool_calls: tuple[ToolCall, ...] = (),
    input_tokens: int = 10,
    output_tokens: int = 5,
    cost_usd: float | None = 0.01,
) -> LLMResponse:
    return LLMResponse(
        message=Message(
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=tool_calls,
        ),
        usage=TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=cost_usd,
        ),
        model_name="deepseek/deepseek-v4-flash",
        finish_reason="tool_calls" if tool_calls else "stop",
    )


class ScriptedLLM(BaseLLM):
    def __init__(self, config: LLMConfig, responses: list[LLMResponse]) -> None:
        super().__init__(config)
        self.responses = responses

    def complete(self, messages, tools=()):
        return self.responses.pop(0)


def test_runner_clones_runs_agent_writes_artifacts_and_converts_cost(tmp_path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    secret = "sk-deepseek-secret-123456"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    patch = """diff --git a/sample.py b/sample.py
--- a/sample.py
+++ b/sample.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""
    scripted = [
        _response(
            None,
            tool_calls=(
                ToolCall(id="patch-1", name="apply_patch", arguments={"patch": patch}),
            ),
        ),
        _response("修复完成", input_tokens=20, output_tokens=4, cost_usd=0.02),
    ]
    seen_config: list[LLMConfig] = []

    def factory(config: LLMConfig) -> BaseLLM:
        seen_config.append(config)
        return ScriptedLLM(config, scripted)

    result = TraceFixRunner(factory).run(
        RunConfig(
            repo=repo,
            task="修改常量",
            output_dir=tmp_path / "runs",
            env_file=None,
            usd_cny_rate=7.2,
        )
    )

    assert result.status is AgentStatus.COMPLETED
    assert result.source_commit == _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert result.workspace is not None
    workspace = Path(result.workspace)
    assert (workspace / "sample.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (repo / "sample.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert result.changed_files == ("sample.py",)
    assert result.input_tokens == 30
    assert result.output_tokens == 9
    assert result.cost_usd == pytest.approx(0.03)
    assert result.cost_cny_estimate == pytest.approx(0.216)
    assert result.cost_complete
    assert result.context_metrics.preparation_count == 2
    assert seen_config[0].extra_kwargs == {
        "api_base": "https://api.deepseek.com",
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    assert "+VALUE = 2" in Path(result.diff_path).read_text(encoding="utf-8")

    result_text = Path(result.result_path).read_text(encoding="utf-8")
    trace_text = Path(result.trace_path).read_text(encoding="utf-8")
    assert secret not in result_text + trace_text
    assert json.loads(result_text)["status"] == "completed"
    assert json.loads(result_text)["context_metrics"]["preparation_count"] == 2
    event_types = [json.loads(line)["event_type"] for line in trace_text.splitlines()]
    assert event_types[0] == "task_started"
    assert event_types[-1] == "task_finished"


def test_runner_marks_unknown_cost_incomplete(tmp_path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-unknown-cost")
    runner = TraceFixRunner(
        lambda config: ScriptedLLM(config, [_response("done", cost_usd=None)])
    )

    result = runner.run(
        RunConfig(repo=repo, task="inspect", output_dir=tmp_path / "runs", env_file=None)
    )

    assert result.status is AgentStatus.COMPLETED
    assert result.cost_usd == 0
    assert not result.cost_complete
    assert result.cost_cny_estimate is None


def test_runner_returns_failed_result_for_dirty_source(tmp_path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    (repo / "dirty.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-dirty-source")

    result = TraceFixRunner().run(
        RunConfig(repo=repo, task="fix", output_dir=tmp_path / "runs", env_file=None)
    )

    assert result.status is AgentStatus.FAILED
    assert result.stop_reason == "workspace_error"
    assert result.workspace is None
    assert result.error["code"] == "workspace_error"
    assert Path(result.result_path).is_file()
    assert Path(result.diff_path).read_text(encoding="utf-8") == ""


def test_runner_loads_dotenv_without_overriding_existing_environment(tmp_path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEEPSEEK_API_KEY=sk-file-value-123456\nTRACEFIX_MODEL=file-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-existing-value-123456")
    observed: list[str | None] = []

    def factory(config: LLMConfig) -> BaseLLM:
        observed.append(os.getenv("DEEPSEEK_API_KEY"))
        return ScriptedLLM(config, [_response("done")])

    result = TraceFixRunner(factory).run(
        RunConfig(repo=repo, task="fix", output_dir=tmp_path / "runs", env_file=env_file)
    )

    assert result.status is AgentStatus.COMPLETED
    assert observed == ["sk-existing-value-123456"]


def test_runner_missing_deepseek_key_is_structured_failure(tmp_path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    result = TraceFixRunner().run(
        RunConfig(repo=repo, task="fix", output_dir=tmp_path / "runs", env_file=None)
    )

    assert result.status is AgentStatus.FAILED
    assert result.error["code"] == "run_configuration_error"
    assert result.cost_complete
    assert result.cost_cny_estimate == 0
    trace = Path(result.trace_path).read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY" in trace


def test_runner_redacts_secret_from_unexpected_provider_message(tmp_path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    secret = "sk-provider-leak-12345678"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)

    class BrokenLLM(BaseLLM):
        def complete(self, messages, tools=()):
            raise LLMProviderError(f"provider rejected {secret}")

    result = TraceFixRunner(BrokenLLM).run(
        RunConfig(repo=repo, task="fix", output_dir=tmp_path / "runs", env_file=None)
    )

    combined = Path(result.result_path).read_text(encoding="utf-8") + Path(
        result.trace_path
    ).read_text(encoding="utf-8")
    assert result.status is AgentStatus.FAILED
    assert secret not in combined
    assert "<redacted>" in combined


def test_run_tests_does_not_inherit_provider_secrets(tmp_path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    (repo / "test_environment.py").write_text(
        "import os\n\ndef test_secret_is_absent():\n"
        "    assert os.getenv('DEEPSEEK_API_KEY') is None\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-must-not-reach-tests")
    tool = RunTestsTool(repo)

    result = tool.execute(
        ToolCall(
            id="env-test",
            name="run_tests",
            arguments={"command": "pytest test_environment.py -q"},
        )
    )

    assert result.success


def test_non_deepseek_model_does_not_require_deepseek_key(tmp_path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    runner = TraceFixRunner(lambda config: ScriptedLLM(config, [_response("done")]))

    result = runner.run(
        RunConfig(
            repo=repo,
            task="fix",
            model_name="provider/model",
            output_dir=tmp_path / "runs",
            env_file=None,
            agent_config=AgentConfig(max_steps=1),
        )
    )

    assert result.status is AgentStatus.COMPLETED


def test_deepseek_provider_kwargs_follow_official_extra_body_shape(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_BASE", "https://gateway.example.test/")

    assert TraceFixRunner._provider_kwargs("deepseek/deepseek-v4-flash") == {
        "api_base": "https://gateway.example.test",
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    assert TraceFixRunner._provider_kwargs("provider/model") == {}


def test_runner_records_keyboard_interrupt_and_diff_collection_failure(
    tmp_path, monkeypatch
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-interrupt-test-123456")

    class InterruptedLLM(BaseLLM):
        def complete(self, messages, tools=()):
            raise KeyboardInterrupt

    interrupted = TraceFixRunner(InterruptedLLM).run(
        RunConfig(repo=repo, task="fix", output_dir=tmp_path / "interrupt", env_file=None)
    )
    assert interrupted.status is AgentStatus.INTERRUPTED
    assert interrupted.stop_reason == "keyboard_interrupt"

    runner = TraceFixRunner(lambda config: ScriptedLLM(config, [_response("done")]))
    monkeypatch.setattr(
        runner,
        "_collect_diff",
        lambda workspace: (_ for _ in ()).throw(RuntimeError("diff broke")),
    )
    failed = runner.run(
        RunConfig(repo=repo, task="fix", output_dir=tmp_path / "diff", env_file=None)
    )
    assert failed.status is AgentStatus.FAILED
    assert failed.error["code"] == "unexpected_run_error"


def test_runtime_validation_errors_are_explicit(tmp_path) -> None:
    from datetime import UTC, datetime, timedelta

    from pydantic import ValidationError

    from tracefix import RunConfigurationError, RunResult, WorkspaceError, load_environment_file

    with pytest.raises(RunConfigurationError):
        load_environment_file(tmp_path)
    with pytest.raises(ValidationError):
        RunConfig(repo=tmp_path, task="   ")
    with pytest.raises(ValidationError):
        RunConfig(repo=tmp_path, task="x", model_name="   ")
    with pytest.raises(WorkspaceError):
        TraceFixRunner._validate_source_repository(tmp_path / "missing")
    with pytest.raises(WorkspaceError):
        TraceFixRunner._validate_source_repository(tmp_path)

    now = datetime.now(UTC)
    base = {
        "run_id": "run",
        "source_repo": str(tmp_path),
        "model_name": "model",
        "status": AgentStatus.FAILED,
        "cost_complete": True,
        "usd_cny_rate": 7.2,
        "started_at": now,
        "finished_at": now,
        "duration_seconds": 0,
        "trace_path": "trace",
        "diff_path": "diff",
        "result_path": "result",
        "agent_config": AgentConfig(),
    }
    with pytest.raises(ValidationError):
        RunResult(**{**base, "finished_at": now - timedelta(seconds=1)})
    with pytest.raises(ValidationError):
        RunResult(**{**base, "cost_complete": False, "cost_cny_estimate": 1.0})


def test_runtime_rejects_a_subdirectory_of_a_git_repository(tmp_path: Path) -> None:
    """显式仓库根约束避免临时目录意外继承外层 Git 仓库。"""
    repo = _make_repo(tmp_path)
    nested = repo / "nested"
    nested.mkdir()

    with pytest.raises(WorkspaceError, match="Git repository root"):
        TraceFixRunner._validate_source_repository(nested)
