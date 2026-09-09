from pathlib import Path

from tracefix import (
    AgentConfig,
    ApplyPatchTool,
    BaseLLM,
    BenchmarkConfig,
    BenchmarkRunner,
    ContextManager,
    LLMConfig,
    LLMResponse,
    Message,
    MessageHistory,
    MessageRole,
    ReadFileTool,
    RunTestsTool,
    TokenUsage,
    ToolCall,
    ToolRegistry,
    TraceFixRunner,
    load_benchmark_tasks,
)
from tracefix.exceptions import BenchmarkError

TASKS_DIR = Path(__file__).parents[1] / "benchmarks" / "tasks"
LONG_TASKS_DIR = Path(__file__).parents[1] / "benchmarks" / "long_context_tasks"
CONTEXT_TASKS_DIR = Path(__file__).parents[1] / "benchmarks" / "context_tasks"
BASELINE_PATH = Path(__file__).parents[1] / "benchmarks" / "baselines" / "v0.2.0.json"
EXPERIMENT_PATH = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "experiments"
    / "v0.3.0-context-3k.json"
)
REGRESSION_PATH = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "experiments"
    / "v0.3.1-agent-loop-regression.json"
)
CONTEXT_AB_PATH = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "experiments"
    / "v0.3.1-context-32k-ab.json"
)
FIXTURE_VALIDATION_PATH = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "experiments"
    / "v0.3.2-fixture-validation.json"
)


def _call(tool, call_id: str, **arguments):
    return ToolCall(id=call_id, name=tool.spec.name, arguments=arguments)


def test_all_benchmark_tasks_fail_initially_and_gold_patch_passes(tmp_path) -> None:
    tasks = load_benchmark_tasks(TASKS_DIR)
    assert len(tasks) == 10

    for task in tasks:
        repo = task.prepare_source_repository(tmp_path / task.id)
        tests = RunTestsTool(repo)
        before = tests.execute(_call(tests, f"before-{task.id}", command=task.test_command))
        assert not before.success, f"{task.id} 初始测试应该失败"

        patcher = ApplyPatchTool(repo)
        applied = patcher.execute(
            _call(
                patcher,
                f"patch-{task.id}",
                patch=task.gold_patch_path.read_text(encoding="utf-8"),
            )
        )
        assert applied.success, f"{task.id}: {applied.error} {applied.output}"
        assert set(applied.output["changed_files"]) == set(task.expected_files)

        after = tests.execute(_call(tests, f"after-{task.id}", command=task.test_command))
        assert after.success, f"{task.id} 标准补丁后应该通过: {after.output}"


def test_long_context_tasks_generate_contracts_and_gold_patch_passes(tmp_path) -> None:
    """长上下文任务应安全生成足量契约，且标准补丁证明任务可解。"""
    tasks = load_benchmark_tasks(LONG_TASKS_DIR)
    assert len(tasks) == 2

    for task in tasks:
        repo = task.prepare_source_repository(tmp_path / task.id)
        contracts = sorted((repo / "contracts").glob("*.md"))
        assert len(contracts) == 24
        assert all(path.stat().st_size > 6_000 for path in contracts)

        tests = RunTestsTool(repo)
        before = tests.execute(_call(tests, f"long-before-{task.id}", command="pytest -q"))
        assert not before.success
        applied = ApplyPatchTool(repo).execute(
            _call(
                ApplyPatchTool(repo),
                f"long-patch-{task.id}",
                patch=task.gold_patch_path.read_text(encoding="utf-8"),
            )
        )
        assert applied.success
        after = tests.execute(_call(tests, f"long-after-{task.id}", command="pytest -q"))
        assert after.success


def test_long_context_fixture_crosses_default_32k_compaction_trigger(tmp_path) -> None:
    """完整读取一题的 24 份契约后，应自然超过生产 32k 软阈值。"""
    task = load_benchmark_tasks(LONG_TASKS_DIR, limit=1)[0]
    repo = task.prepare_source_repository(tmp_path / task.id)
    reader = ReadFileTool(repo)
    history = MessageHistory(
        [
            Message(role=MessageRole.SYSTEM, content="测试系统提示"),
            Message(role=MessageRole.USER, content=task.description),
        ]
    )
    # 与真实任务约束一致：每轮读取四份，形成六个可整体折叠的工具批次。
    for batch_start in range(1, 25, 4):
        calls = tuple(
            ToolCall(
                id=f"read-contract-{index}",
                name="read_file",
                arguments={"path": f"contracts/region_{index:02d}.md"},
            )
            for index in range(batch_start, batch_start + 4)
        )
        history.append(Message(role=MessageRole.ASSISTANT, tool_calls=calls))
        for call in calls:
            result = reader.execute(call)
            history.append(
                Message(
                    role=MessageRole.TOOL,
                    content=result.model_dump_json(),
                    tool_call_id=call.id,
                )
            )

    view = ContextManager().prepare(
        history.snapshot(),
        ToolRegistry([reader]).specs(),
    )

    assert view.estimated_tokens_before > 32_000
    assert view.tool_results_pruned == 0
    assert view.compacted is True
    assert view.estimated_tokens_after < view.estimated_tokens_before


def test_multifile_context_tasks_fail_initially_and_pass_visible_and_hidden_tests(
    tmp_path,
) -> None:
    """四道任务必须真实跨文件，并由 Agent 不可见的测试约束完整语义。"""
    import shutil

    tasks = load_benchmark_tasks(CONTEXT_TASKS_DIR)
    assert len(tasks) == 4
    assert len({task.description for task in tasks}) == 4

    for task in tasks:
        repo = task.prepare_source_repository(tmp_path / task.id)
        assert task.hidden_tests_path is not None
        assert len(list(repo.rglob("*.py"))) >= 5
        tests = RunTestsTool(repo)
        before = tests.execute(_call(tests, f"before-{task.id}", command="pytest -q"))
        assert not before.success, f"{task.id} 初始测试应该失败"

        patcher = ApplyPatchTool(repo)
        applied = patcher.execute(
            _call(
                patcher,
                f"patch-{task.id}",
                patch=task.gold_patch_path.read_text(encoding="utf-8"),
            )
        )
        assert applied.success, f"{task.id}: {applied.error} {applied.output}"
        shutil.copytree(task.hidden_tests_path, repo / ".tracefix_verification")
        after = tests.execute(
            _call(
                tests,
                f"after-{task.id}",
                command="pytest -q tests .tracefix_verification",
            )
        )
        assert after.success, f"{task.id} 标准补丁后应该通过: {after.output}"


def test_checked_in_baseline_is_complete_and_sanitized() -> None:
    """版本化 Baseline 只包含复现实验所需的脱敏聚合数据。"""
    import json

    payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    serialized = json.dumps(payload).casefold()
    assert payload["tracefix_version"] == "0.2.0"
    assert len(payload["tasks"]) == 10
    assert sum(item["input_tokens"] for item in payload["tasks"]) == 184_730
    assert sum(item["output_tokens"] for item in payload["tasks"]) == 8_982
    assert "d:\\tracefix" not in serialized
    assert "api_key" not in serialized


def test_checked_in_context_experiment_is_consistent_and_sanitized() -> None:
    """压力实验只固化指标与结论，不提交本机路径或原始模型内容。"""
    import json

    payload = json.loads(EXPERIMENT_PATH.read_text(encoding="utf-8"))
    serialized = json.dumps(payload).casefold()
    aggregate = payload["aggregate"]
    assert payload["tracefix_version"] == "0.3.0"
    assert payload["context"]["compaction_trigger_tokens"] == 3_000
    assert len(payload["tasks"]) == 10
    assert sum(item["resolved"] for item in payload["tasks"]) == 7
    assert sum(item["input_tokens"] for item in payload["tasks"]) == 203_390
    assert sum(item["tool_calls"] for item in payload["tasks"]) == 101
    assert aggregate["exact_duplicate_tool_calls"] == 23
    assert aggregate["context_metrics"]["estimated_tokens_saved"] == 17_973
    assert "d:\\tracefix" not in serialized
    assert "api_key" not in serialized
    assert "trajectory.jsonl" not in serialized


def test_checked_in_agent_loop_regression_is_consistent_and_sanitized() -> None:
    """V0.3.1 回归记录必须与聚合值一致，并排除本机运行信息。"""
    import json

    payload = json.loads(REGRESSION_PATH.read_text(encoding="utf-8"))
    serialized = json.dumps(payload).casefold()
    aggregate = payload["aggregate"]
    tasks = payload["tasks"]
    assert payload["tracefix_version"] == "0.3.1"
    assert len(tasks) == 10
    assert sum(item["resolved"] for item in tasks) == 10
    assert sum(item["input_tokens"] for item in tasks) == aggregate["input_tokens"]
    assert sum(item["output_tokens"] for item in tasks) == aggregate["output_tokens"]
    assert sum(item["tool_calls"] for item in tasks) == aggregate["tool_calls"]
    assert aggregate["failed_tool_calls"] == 0
    assert aggregate["context_metrics"]["compaction_count"] == 0
    assert "d:\\tracefix" not in serialized
    assert "api_key" not in serialized
    assert "trajectory.jsonl" not in serialized


def test_checked_in_32k_ab_experiment_is_consistent_and_sanitized() -> None:
    """A/B 记录的聚合值、配对任务和脱敏边界必须保持可复核。"""
    import json

    payload = json.loads(CONTEXT_AB_PATH.read_text(encoding="utf-8"))
    serialized = json.dumps(payload).casefold()
    control = payload["control"]
    treatment = payload["treatment"]
    assert payload["tracefix_commit"] == "247a88121c7a35acc52fd9b1809e90f07949b6d9"
    assert control["resolved_count"] == treatment["resolved_count"] == 2
    assert [item["id"] for item in control["tasks"]] == [
        item["id"] for item in treatment["tasks"]
    ]
    assert sum(item["input_tokens"] for item in control["tasks"]) == 370_387
    assert sum(item["input_tokens"] for item in treatment["tasks"]) == 185_587
    assert treatment["context_metrics"]["compaction_count"] == 5
    assert payload["aggregate_effect"]["input_tokens_delta"] == -184_800
    assert "d:\\tracefix" not in serialized
    assert "api_key" not in serialized
    assert "trajectory.jsonl" not in serialized


def test_checked_in_multifile_fixture_validation_is_consistent() -> None:
    """离线记录必须明确区分 gold 验收与真实 Agent 解决率。"""
    import json

    payload = json.loads(FIXTURE_VALIDATION_PATH.read_text(encoding="utf-8"))
    tasks = payload["tasks"]
    assert payload["kind"] == "offline_fixture_validation"
    assert payload["model_calls"] == 0
    assert len(tasks) == 4
    assert all(item["initial_failed"] for item in tasks)
    assert all(item["gold_applied"] for item in tasks)
    assert all(item["visible_and_hidden_passed"] for item in tasks)
    assert payload["aggregate"]["visible_and_hidden_pass_count"] == 4


class GoldPatchLLM(BaseLLM):
    def __init__(self, config: LLMConfig, patch: str) -> None:
        super().__init__(config)
        self.patch = patch
        self.calls = 0

    def complete(self, messages, tools=()):
        self.calls += 1
        if self.calls == 1:
            message = Message(
                role=MessageRole.ASSISTANT,
                tool_calls=(
                    ToolCall(
                        id="gold-patch",
                        name="apply_patch",
                        arguments={"patch": self.patch},
                    ),
                ),
            )
        else:
            message = Message(role=MessageRole.ASSISTANT, content="修复完成")
        return LLMResponse(
            message=message,
            usage=TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5, cost_usd=0.001),
            model_name=self.config.model_name,
        )


def test_benchmark_runner_limits_tasks_verifies_patch_and_writes_summary(
    tmp_path, monkeypatch
) -> None:
    task = load_benchmark_tasks(TASKS_DIR, limit=1)[0]
    patch = task.gold_patch_path.read_text(encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-benchmark-test-123456")
    runner = TraceFixRunner(lambda config: GoldPatchLLM(config, patch))

    summary = BenchmarkRunner(runner).run(
        BenchmarkConfig(
            tasks_dir=TASKS_DIR,
            output_dir=tmp_path / "runs",
            limit=1,
            env_file=None,
            agent_config=AgentConfig(max_steps=4, max_test_runs=2),
        )
    )

    assert summary.task_count == 1
    assert summary.resolved_count == 1
    assert summary.resolved_rate == 1
    assert summary.total_input_tokens == 6
    assert summary.total_output_tokens == 4
    assert summary.total_cost_cny_estimate == 0.0144
    assert summary.context_metrics.preparation_count == 2
    assert summary.results[0].verification.success
    assert Path(summary.summary_path).is_file()


def test_benchmark_runner_uses_hidden_tests_without_exposing_them_to_agent(
    tmp_path, monkeypatch
) -> None:
    """隐藏测试仅在最终验证阶段出现，并明确标注判定类型。"""
    task = load_benchmark_tasks(CONTEXT_TASKS_DIR, limit=1)[0]
    patch = task.gold_patch_path.read_text(encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-hidden-benchmark-test-123456")
    runner = TraceFixRunner(lambda config: GoldPatchLLM(config, patch))

    summary = BenchmarkRunner(runner).run(
        BenchmarkConfig(
            tasks_dir=CONTEXT_TASKS_DIR,
            output_dir=tmp_path / "hidden-runs",
            limit=1,
            env_file=None,
            agent_config=AgentConfig(max_steps=4, max_test_runs=2),
        )
    )

    result = summary.results[0]
    assert result.resolved is True
    assert result.agent_completed is True
    assert result.public_tests_passed is True
    assert result.independent_tests_passed is True
    assert result.tests_modified is False
    assert result.verification_kind == "visible_and_hidden"
    assert result.public_verification.metadata["verification_kind"] == "public"
    assert result.independent_verification.metadata["verification_kind"] == (
        "independent_hidden"
    )
    workspace = Path(result.run.workspace)
    verification_dirs = list(workspace.glob(".tracefix_verification_*"))
    assert len(verification_dirs) == 1 and verification_dirs[0].is_dir()
    # 最终 patch 在隐藏测试复制前收集，不会把评测答案混进 Agent 产物。
    assert ".tracefix_verification" not in Path(result.run.diff_path).read_text(
        encoding="utf-8"
    )


def test_test_file_and_pytest_configuration_changes_are_detected(tmp_path, monkeypatch) -> None:
    """测试或发现配置的修改必须单独暴露，不能只看 pytest 退出码。"""
    task = load_benchmark_tasks(CONTEXT_TASKS_DIR, limit=1)[0]
    patch = task.gold_patch_path.read_text(encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-change-detection-123456")
    runner = TraceFixRunner(lambda config: GoldPatchLLM(config, patch))
    summary = BenchmarkRunner(runner).run(
        BenchmarkConfig(
            tasks_dir=CONTEXT_TASKS_DIR,
            output_dir=tmp_path / "runs",
            limit=1,
            env_file=None,
        )
    )
    changed_run = summary.results[0].run.model_copy(
        update={
            "changed_files": (
                "config/resolver.py",
                "tests/test_resolver.py",
                "tests/__pycache__/test_resolver.cpython-312.pyc",
                "pyproject.toml",
            )
        }
    )
    assert BenchmarkRunner._changed_test_files(task, changed_run) == (
        "pyproject.toml",
        "tests/test_resolver.py",
    )


def test_modified_public_test_prevents_resolved_even_when_all_tests_pass(
    tmp_path, monkeypatch
) -> None:
    """测试篡改是独立否决条件，不能被公开或隐藏测试成功覆盖。"""
    task = load_benchmark_tasks(CONTEXT_TASKS_DIR, limit=1)[0]
    gold = task.gold_patch_path.read_text(encoding="utf-8")
    test_patch = """*** Begin Patch
*** Update File: tests/test_resolver.py
@@
 from config import resolve_config
+# Agent 不应修改测试，即使只是无害注释。
*** End Patch"""

    class TestModifyingLLM(BaseLLM):
        def __init__(self, config):
            super().__init__(config)
            self.calls = 0

        def complete(self, messages, tools=()):
            self.calls += 1
            calls = ()
            if self.calls == 1:
                calls = (
                    ToolCall(id="source", name="apply_patch", arguments={"patch": gold}),
                    ToolCall(id="test", name="apply_patch", arguments={"patch": test_patch}),
                )
            return LLMResponse(
                message=Message(
                    role=MessageRole.ASSISTANT,
                    content=None if calls else "完成",
                    tool_calls=calls,
                ),
                usage=TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5),
                model_name=self.config.model_name,
            )

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-modification-123456")
    summary = BenchmarkRunner(TraceFixRunner(TestModifyingLLM)).run(
        BenchmarkConfig(
            tasks_dir=CONTEXT_TASKS_DIR,
            output_dir=tmp_path / "mutation-runs",
            limit=1,
            env_file=None,
        )
    )
    result = summary.results[0]
    assert result.agent_completed
    assert result.public_tests_passed
    assert result.independent_tests_passed
    assert result.tests_modified
    assert result.changed_test_files == ("tests/test_resolver.py",)
    assert not result.resolved
    assert summary.tests_modified_count == 1
    assert summary.resolved_count == 0


def test_benchmark_loader_rejects_missing_unknown_and_unsafe_tasks(tmp_path) -> None:
    import json

    import pytest

    with pytest.raises(BenchmarkError):
        load_benchmark_tasks(tmp_path / "missing")
    with pytest.raises(BenchmarkError):
        load_benchmark_tasks(TASKS_DIR, task_ids=("unknown",))

    task_dir = tmp_path / "unsafe"
    task_dir.mkdir()
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "id": "unsafe",
                "title": "unsafe",
                "description": "unsafe",
                "expected_files": ["../secret.py"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkError):
        load_benchmark_tasks(tmp_path)


def test_benchmark_task_checks_artifacts_and_existing_destination(tmp_path) -> None:
    import json

    import pytest

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    manifest = {
        "id": "fixture",
        "title": "fixture",
        "description": "fixture",
        "expected_files": ["module.py"],
    }
    (task_dir / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BenchmarkError, match="repository"):
        load_benchmark_tasks(tmp_path)

    (task_dir / "repo").mkdir()
    with pytest.raises(BenchmarkError, match="gold patch"):
        load_benchmark_tasks(tmp_path)

    (task_dir / "gold.patch").write_text("patch", encoding="utf-8")
    task = load_benchmark_tasks(tmp_path)[0]
    destination = tmp_path / "exists"
    destination.mkdir()
    with pytest.raises(BenchmarkError, match="already exists"):
        task.prepare_source_repository(destination)
