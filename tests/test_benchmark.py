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
