import json
from pathlib import Path

from tracefix import (
    AgentConfig,
    BaseLLM,
    BenchmarkConfig,
    BenchmarkRunner,
    ExperimentArm,
    LLMConfig,
    LLMResponse,
    Message,
    MessageRole,
    PairedExperimentConfig,
    PairedExperimentRunner,
    TokenUsage,
    ToolCall,
    TraceFixRunner,
    analyze_trajectory,
    load_benchmark_tasks,
)

CONTEXT_TASKS_DIR = Path(__file__).parents[1] / "benchmarks" / "context_tasks"


class _GoldPatchLLM(BaseLLM):
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
                        id="patch",
                        name="apply_patch",
                        arguments={"patch": self.patch},
                    ),
                ),
            )
        else:
            message = Message(role=MessageRole.ASSISTANT, content="完成")
        return LLMResponse(
            message=message,
            usage=TokenUsage(
                input_tokens=5,
                output_tokens=2,
                total_tokens=7,
                cost_usd=0.001,
            ),
            model_name=self.config.model_name,
        )


def test_analyze_trajectory_counts_rereads_duplicates_and_post_compaction_failures(
    tmp_path,
) -> None:
    path = tmp_path / "trajectory.jsonl"
    events = [
        {
            "event_type": "tool_called",
            "step": 1,
            "payload": {"call": {"name": "read_file", "arguments": {"path": "a.py"}}},
        },
        {
            "event_type": "tool_called",
            "step": 2,
            "payload": {"call": {"name": "read_file", "arguments": {"path": "a.py"}}},
        },
        {
            "event_type": "context_compacted",
            "step": 3,
            "payload": {"batches_compacted": 1, "tool_results_pruned": 2},
        },
        {
            "event_type": "tool_returned",
            "step": 3,
            "payload": {"result": {"success": False}},
        },
    ]
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    metrics = analyze_trajectory(path)
    assert metrics.tool_calls == 2
    assert metrics.duplicate_tool_calls == 1
    assert metrics.file_rereads == 1
    assert metrics.compaction_count == 1
    assert metrics.tool_result_pruning_events == 1
    assert metrics.failed_tool_calls == 1
    assert metrics.post_compaction_failed_tool_calls == 1


def test_paired_runner_alternates_arms_and_writes_partial_summary(tmp_path, monkeypatch) -> None:
    """三次重复采用 C/T、T/C、C/T，并保持模型、任务与预算一致。"""
    task = load_benchmark_tasks(CONTEXT_TASKS_DIR, limit=1)[0]
    patch = task.gold_patch_path.read_text(encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-paired-test-123456")
    benchmark_runner = BenchmarkRunner(
        TraceFixRunner(lambda config: _GoldPatchLLM(config, patch))
    )
    summary = PairedExperimentRunner(benchmark_runner).run(
        PairedExperimentConfig(
            benchmark=BenchmarkConfig(
                tasks_dir=CONTEXT_TASKS_DIR,
                output_dir=tmp_path / "paired",
                limit=1,
                env_file=None,
                agent_config=AgentConfig(max_steps=3),
            ),
            repetitions=3,
        )
    )

    assert [trial.arm for trial in summary.trials] == [
        ExperimentArm.CONTROL,
        ExperimentArm.TREATMENT,
        ExperimentArm.TREATMENT,
        ExperimentArm.CONTROL,
        ExperimentArm.CONTROL,
        ExperimentArm.TREATMENT,
    ]
    assert summary.control.trial_count == summary.treatment.trial_count == 3
    assert summary.control.resolved_count == summary.treatment.resolved_count == 3
    assert summary.treatment.compaction_triggered_trials == 0
    assert summary.treatment_not_triggered_task_ids == ("config_precedence",)
    assert Path(summary.summary_path).is_file()
    assert all(trial.result.run.agent_config.record_request_views for trial in summary.trials)
