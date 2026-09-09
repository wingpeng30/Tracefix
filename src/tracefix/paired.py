"""关闭压缩与 32k 压缩的交替、重复配对实验。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from tracefix.agent import AgentConfig
from tracefix.benchmark import (
    BenchmarkConfig,
    BenchmarkRunner,
    BenchmarkTaskResult,
    load_benchmark_tasks,
)
from tracefix.context import ContextConfig


class ExperimentArm(StrEnum):
    """配对实验中唯一允许变化的上下文策略。"""

    CONTROL = "no_compaction"
    TREATMENT = "compaction_32k"


class TrajectoryMetrics(BaseModel):
    """从 JSONL 轨迹确定性计算的行为指标。"""

    model_config = ConfigDict(extra="forbid")

    tool_calls: int = Field(default=0, ge=0)
    duplicate_tool_calls: int = Field(default=0, ge=0)
    file_rereads: int = Field(default=0, ge=0)
    failed_tool_calls: int = Field(default=0, ge=0)
    tool_result_pruning_events: int = Field(default=0, ge=0)
    compaction_count: int = Field(default=0, ge=0)
    post_compaction_failed_tool_calls: int = Field(default=0, ge=0)


class PairedTrial(BaseModel):
    """一道题、一个重复编号和一种策略的完整结果。"""

    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    repetition: int = Field(ge=1)
    arm: ExperimentArm
    task_id: str
    result: BenchmarkTaskResult
    trajectory_metrics: TrajectoryMetrics


class ArmAggregate(BaseModel):
    """一种上下文策略的聚合质量、成本与行为指标。"""

    model_config = ConfigDict(extra="forbid")

    trial_count: int = Field(ge=0)
    resolved_count: int = Field(ge=0)
    agent_completed_count: int = Field(ge=0)
    public_passed_count: int = Field(ge=0)
    independent_passed_count: int = Field(ge=0)
    tests_modified_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    cost_cny_estimate: float | None = Field(default=None, ge=0)
    tool_calls: int = Field(ge=0)
    duplicate_tool_calls: int = Field(ge=0)
    file_rereads: int = Field(ge=0)
    failed_tool_calls: int = Field(ge=0)
    compaction_triggered_trials: int = Field(ge=0)
    tool_result_pruning_events: int = Field(ge=0)
    post_compaction_failed_tool_calls: int = Field(ge=0)
    unresolved_after_compaction_count: int = Field(ge=0)


class PairedExperimentConfig(BaseModel):
    """固定模型与预算、只改变压缩开关的实验配置。"""

    model_config = ConfigDict(extra="forbid")

    benchmark: BenchmarkConfig
    repetitions: int = Field(default=3, ge=3)
    trigger_tokens: int = Field(default=32_000, ge=1)


class PairedExperimentSummary(BaseModel):
    """可在每个 trial 后恢复读取的配对实验汇总。"""

    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    started_at: datetime
    finished_at: datetime
    repetitions: int
    model_name: str
    trigger_tokens: int
    trials: tuple[PairedTrial, ...]
    control: ArmAggregate
    treatment: ArmAggregate
    treatment_triggered_task_ids: tuple[str, ...]
    treatment_not_triggered_task_ids: tuple[str, ...]
    summary_path: str


class PairedExperimentRunner:
    """按 C/T、T/C 交替次序执行配对实验，并持续保存中间结果。"""

    def __init__(self, benchmark_runner: BenchmarkRunner | None = None) -> None:
        self.benchmark_runner = benchmark_runner or BenchmarkRunner()

    def run(self, config: PairedExperimentConfig) -> PairedExperimentSummary:
        """执行每题每组至少三次的同版本配对实验。"""
        tasks = load_benchmark_tasks(
            config.benchmark.tasks_dir,
            task_ids=config.benchmark.task_ids,
            limit=config.benchmark.limit,
        )
        experiment_id = (
            f"paired-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{uuid4().hex[:8]}"
        )
        experiment_dir = config.benchmark.output_dir.resolve() / experiment_id
        artifacts_dir = experiment_dir / "artifacts"
        summary_path = experiment_dir / "paired-summary.json"
        experiment_dir.mkdir(parents=True, exist_ok=False)
        started_at = datetime.now(UTC)
        trials: list[PairedTrial] = []

        for repetition in range(1, config.repetitions + 1):
            # 奇数轮 C→T，偶数轮 T→C，抵消时间、网络和缓存顺序偏差。
            arms = (
                (ExperimentArm.CONTROL, ExperimentArm.TREATMENT)
                if repetition % 2
                else (ExperimentArm.TREATMENT, ExperimentArm.CONTROL)
            )
            for task in tasks:
                for arm in arms:
                    agent_config = self._agent_config(config, arm)
                    batch = self.benchmark_runner.run(
                        config.benchmark.model_copy(
                            update={
                                "output_dir": artifacts_dir,
                                "limit": None,
                                "task_ids": (task.id,),
                                "agent_config": agent_config,
                            }
                        )
                    )
                    result = batch.results[0]
                    trials.append(
                        PairedTrial(
                            sequence=len(trials) + 1,
                            repetition=repetition,
                            arm=arm,
                            task_id=task.id,
                            result=result,
                            trajectory_metrics=analyze_trajectory(
                                Path(result.run.trace_path)
                            ),
                        )
                    )
                    summary = self._build_summary(
                        config,
                        experiment_id,
                        started_at,
                        trials,
                        tuple(task.id for task in tasks),
                        summary_path,
                    )
                    summary_path.write_text(
                        summary.model_dump_json(indent=2), encoding="utf-8"
                    )
        return summary

    @staticmethod
    def _agent_config(
        config: PairedExperimentConfig, arm: ExperimentArm
    ) -> AgentConfig:
        """复制全部预算，仅覆盖上下文策略并强制记录脱敏请求视图。"""
        original = config.benchmark.agent_config
        context = ContextConfig.model_validate(
            {
                **original.context.model_dump(mode="python"),
                "enabled": arm is ExperimentArm.TREATMENT,
                "compaction_trigger_tokens": config.trigger_tokens,
            }
        )
        return original.model_copy(
            update={"context": context, "record_request_views": True}
        )

    @classmethod
    def _build_summary(
        cls,
        config: PairedExperimentConfig,
        experiment_id: str,
        started_at: datetime,
        trials: list[PairedTrial],
        task_ids: tuple[str, ...],
        summary_path: Path,
    ) -> PairedExperimentSummary:
        treatment_tasks = {
            trial.task_id
            for trial in trials
            if trial.arm is ExperimentArm.TREATMENT
            and trial.trajectory_metrics.compaction_count > 0
        }
        attempted_treatment_tasks = {
            trial.task_id
            for trial in trials
            if trial.arm is ExperimentArm.TREATMENT
        }
        return PairedExperimentSummary(
            experiment_id=experiment_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            repetitions=config.repetitions,
            model_name=config.benchmark.model_name,
            trigger_tokens=config.trigger_tokens,
            trials=tuple(trials),
            control=cls._aggregate(trials, ExperimentArm.CONTROL, config),
            treatment=cls._aggregate(trials, ExperimentArm.TREATMENT, config),
            treatment_triggered_task_ids=tuple(sorted(treatment_tasks)),
            treatment_not_triggered_task_ids=tuple(
                sorted(attempted_treatment_tasks.difference(treatment_tasks))
            ),
            summary_path=str(summary_path),
        )

    @staticmethod
    def _aggregate(
        trials: list[PairedTrial],
        arm: ExperimentArm,
        config: PairedExperimentConfig,
    ) -> ArmAggregate:
        selected = [trial for trial in trials if trial.arm is arm]
        cost_complete = all(trial.result.run.cost_complete for trial in selected)
        usd = sum(trial.result.run.cost_usd for trial in selected)
        return ArmAggregate(
            trial_count=len(selected),
            resolved_count=sum(trial.result.resolved for trial in selected),
            agent_completed_count=sum(trial.result.agent_completed for trial in selected),
            public_passed_count=sum(trial.result.public_tests_passed for trial in selected),
            independent_passed_count=sum(
                trial.result.independent_tests_passed is True for trial in selected
            ),
            tests_modified_count=sum(trial.result.tests_modified for trial in selected),
            input_tokens=sum(trial.result.run.input_tokens for trial in selected),
            output_tokens=sum(trial.result.run.output_tokens for trial in selected),
            cost_usd=usd,
            cost_cny_estimate=(
                round(usd * config.benchmark.usd_cny_rate, 8)
                if cost_complete
                else None
            ),
            tool_calls=sum(trial.trajectory_metrics.tool_calls for trial in selected),
            duplicate_tool_calls=sum(
                trial.trajectory_metrics.duplicate_tool_calls for trial in selected
            ),
            file_rereads=sum(trial.trajectory_metrics.file_rereads for trial in selected),
            failed_tool_calls=sum(
                trial.trajectory_metrics.failed_tool_calls for trial in selected
            ),
            compaction_triggered_trials=sum(
                trial.trajectory_metrics.compaction_count > 0 for trial in selected
            ),
            tool_result_pruning_events=sum(
                trial.trajectory_metrics.tool_result_pruning_events
                for trial in selected
            ),
            post_compaction_failed_tool_calls=sum(
                trial.trajectory_metrics.post_compaction_failed_tool_calls
                for trial in selected
            ),
            unresolved_after_compaction_count=sum(
                not trial.result.resolved
                and trial.trajectory_metrics.compaction_count > 0
                for trial in selected
            ),
        )


def analyze_trajectory(path: Path) -> TrajectoryMetrics:
    """分析重读、重复、失败和压缩后的失败决策，不依赖模型思考文本。"""
    seen_signatures: set[str] = set()
    seen_read_paths: set[str] = set()
    metrics = TrajectoryMetrics()
    first_compaction_step: int | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        event_type = event.get("event_type")
        step = event.get("step")
        if event_type == "context_compacted":
            payload = event.get("payload", {})
            if int(payload.get("tool_results_pruned", 0) or 0) > 0:
                metrics.tool_result_pruning_events += 1
            # 只有折叠了完整 assistant/tool 批次才算真正历史折叠；单条工具
            # 结果裁剪必须单独统计，不能夸大 32k 机制的触发次数。
            if int(payload.get("batches_compacted", 0) or 0) > 0:
                metrics.compaction_count += 1
                if first_compaction_step is None and isinstance(step, int):
                    first_compaction_step = step
        elif event_type == "tool_called":
            call = event.get("payload", {}).get("call", {})
            name = str(call.get("name", ""))
            arguments = call.get("arguments", {})
            signature = f"{name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"
            metrics.tool_calls += 1
            if signature in seen_signatures:
                metrics.duplicate_tool_calls += 1
            seen_signatures.add(signature)
            if name == "read_file":
                read_path = str(arguments.get("path", ""))
                if read_path in seen_read_paths:
                    metrics.file_rereads += 1
                seen_read_paths.add(read_path)
        elif event_type == "tool_returned":
            result = event.get("payload", {}).get("result", {})
            if result.get("success") is False:
                metrics.failed_tool_calls += 1
                if (
                    first_compaction_step is not None
                    and isinstance(step, int)
                    and step >= first_compaction_step
                ):
                    metrics.post_compaction_failed_tool_calls += 1
    return metrics
