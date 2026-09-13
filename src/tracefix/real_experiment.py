"""真实 GitHub Issue 的行为校验、32k 预筛选与条件配对实验。"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from tracefix.agent import AgentConfig, AgentStatus
from tracefix.context import ContextConfig
from tracefix.exceptions import BenchmarkError
from tracefix.messages import ToolCall
from tracefix.paired import ExperimentArm, TrajectoryMetrics, analyze_trajectory
from tracefix.provenance import TestEnvironmentProvenance, inspect_test_environment
from tracefix.real_benchmark import RealIssueTask, load_real_issue_tasks
from tracefix.runtime import (
    DEFAULT_MODEL_NAME,
    DEFAULT_USD_CNY_RATE,
    RunConfig,
    RunResult,
    TraceFixRunner,
)
from tracefix.tools import RunTestsTool, ToolResult


class RealTaskBehaviorValidation(BaseModel):
    """原始提交失败、标准补丁通过以及独立测试环境的可复现证据。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    base_commit: str
    initial_hidden_failed: bool
    gold_hidden_passed: bool
    initial_returncode: int | None
    gold_returncode: int | None
    test_command: str
    test_environment: TestEnvironmentProvenance


class RealTrajectoryMetrics(BaseModel):
    """预筛选所需的上下文规模、文件覆盖和闭环行为指标。"""

    model_config = ConfigDict(extra="forbid")

    behavior: TrajectoryMetrics = Field(default_factory=TrajectoryMetrics)
    max_estimated_tokens_before: int = Field(default=0, ge=0)
    max_estimated_tokens_after: int = Field(default=0, ge=0)
    unique_read_files: tuple[str, ...] = ()
    meaningful_read_files: tuple[str, ...] = ()
    apply_patch_calls: int = Field(default=0, ge=0)
    run_tests_calls: int = Field(default=0, ge=0)
    last_agent_test_passed: bool | None = None
    request_views_recorded: int = Field(default=0, ge=0)
    # 这些字段只在评测端根据任务清单计算，不会传给 Agent，避免泄露标准答案。
    first_target_read_step: int | None = Field(default=None, ge=0)
    first_target_read_tool_call: int | None = Field(default=None, ge=1)
    first_target_read_path: str | None = None
    first_patch_step: int | None = Field(default=None, ge=0)
    first_test_step: int | None = Field(default=None, ge=0)
    post_target_search_calls: int = Field(default=0, ge=0)
    cached_tool_calls: int = Field(default=0, ge=0)
    phase_transitions: tuple[str, ...] = ()


class RealTrialResult(BaseModel):
    """一次真实 Issue 运行及 Agent 不可见的独立验收结果。"""

    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    task_id: str
    arm: ExperimentArm
    # 对上下文实验它只是运行配置的补充；对 Repo Map 预筛选则是唯一变量。
    repo_map_enabled: bool = True
    repetition: int = Field(default=1, ge=1)
    run: RunResult
    agent_completed: bool
    # SWE-bench 的 test.patch 对 Agent 不可见，因此不能把 Agent 最后一次自选
    # run_tests 误标为“公开测试通过”。两个状态单独保存，避免缩小测试范围后产生
    # 虚假的成功信号。
    agent_selected_tests_passed: bool | None = None
    public_tests_passed: bool | None = None
    independent_tests_passed: bool
    source_patch_applied: bool = False
    tests_modified: bool
    changed_test_files: tuple[str, ...] = ()
    resolved: bool
    verification: ToolResult | None = None
    trajectory: RealTrajectoryMetrics
    eligible_for_paired: bool
    eligibility_failures: tuple[str, ...] = ()


class RealPrescreenSummary(BaseModel):
    """真实任务 32k 单次预筛选汇总；不把它解释成策略优劣实验。"""

    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    started_at: datetime
    finished_at: datetime
    model_name: str
    trigger_tokens: int
    results: tuple[RealTrialResult, ...]
    eligible_task_ids: tuple[str, ...]
    ineligible_task_ids: tuple[str, ...]
    eligible_for_formal_experiment: bool
    summary_path: str


class RepoMapArmAggregate(BaseModel):
    """Repo Map 开关组的可比较聚合指标，避免把单次结果误读为结论。"""

    model_config = ConfigDict(extra="forbid")

    trial_count: int = Field(ge=0)
    agent_completed_count: int = Field(ge=0)
    public_tests_passed_count: int = Field(ge=0)
    independent_tests_passed_count: int = Field(ge=0)
    resolved_count: int = Field(ge=0)
    tests_modified_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    cost_cny_estimate: float | None = Field(default=None, ge=0)
    target_read_count: int = Field(ge=0)
    average_first_target_read_step: float | None = Field(default=None, ge=0)


class RealRepoMapPrescreenSummary(BaseModel):
    """同一真实任务各跑一次 Repo Map 关闭/开启组的低成本预筛选。"""

    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    started_at: datetime
    finished_at: datetime
    model_name: str
    trigger_tokens: int
    trials: tuple[RealTrialResult, ...]
    control: RepoMapArmAggregate
    treatment: RepoMapArmAggregate
    summary_path: str


class RealExperimentConfig(BaseModel):
    """真实任务运行的固定源码、测试环境、模型和预算。"""

    model_config = ConfigDict(extra="forbid")

    tasks_dir: Path = Path("benchmarks/real_tasks")
    source_root: Path = Path("runs/real-task-validation")
    test_env_root: Path = Path("runs/real-task-envs-v2")
    output_dir: Path = Path("runs")
    task_ids: tuple[str, ...] = ()
    model_name: str = DEFAULT_MODEL_NAME
    env_file: Path | None = Path(".env")
    usd_cny_rate: float = Field(default=DEFAULT_USD_CNY_RATE, gt=0)
    trigger_tokens: int = Field(default=32_000, ge=1)
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    per_request_output_tokens: int = Field(default=4_096, ge=1)
    agent_config: AgentConfig = Field(
        default_factory=lambda: AgentConfig(
            max_steps=24,
            # 与 CLI 真实任务命令保持同一默认口径；调用方仍可显式提高该上限。
            max_input_tokens=350_000,
            max_output_tokens=20_000,
            wall_time_seconds=900,
            max_test_runs=6,
        )
    )


def validate_real_task_behavior(
    task: RealIssueTask,
    *,
    source: Path,
    test_python: Path,
    output_dir: Path,
) -> RealTaskBehaviorValidation:
    """在两个独立副本中证明隐藏用例先失败、应用 gold 后通过。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    task.validate_checkout(source)
    results: dict[str, ToolResult] = {}
    for variant in ("initial", "gold"):
        checkout = output_dir / f"{task.id}-{variant}"
        if checkout.exists():
            raise BenchmarkError(
                "behavior validation destination already exists",
                context={"task_id": task.id, "variant": variant},
            )
        _git(["clone", "--quiet", "--no-hardlinks", str(source), str(checkout)], source.parent)
        _git(["apply", str(task.test_patch_path)], checkout)
        if variant == "gold":
            _git(["apply", str(task.gold_patch_path)], checkout)
        tool = RunTestsTool(
            checkout,
            python_executable=test_python,
            pythonpath_entries=task.test_pythonpath_paths,
            default_timeout_seconds=300,
        )
        results[variant] = tool.execute(
            ToolCall(
                id=f"behavior-{task.id}-{variant}",
                name=tool.spec.name,
                arguments={"command": task.test_command, "timeout_seconds": 300},
            )
        )
    return RealTaskBehaviorValidation(
        task_id=task.id,
        base_commit=task.base_commit,
        initial_hidden_failed=not results["initial"].success,
        gold_hidden_passed=results["gold"].success,
        initial_returncode=_returncode(results["initial"]),
        gold_returncode=_returncode(results["gold"]),
        test_command=task.test_command,
        test_environment=inspect_test_environment(
            test_python, pythonpath_entries=task.test_pythonpath_paths
        ),
    )


class RealPrescreenRunner:
    """逐题执行一次 32k 压缩组，并按预先声明的门槛筛选任务。"""

    def __init__(self, runner: TraceFixRunner | None = None) -> None:
        self.runner = runner or TraceFixRunner()

    def run(self, config: RealExperimentConfig) -> RealPrescreenSummary:
        """运行预筛选；每题后覆盖写 summary，意外中断也能保留已完成结果。"""
        tasks = load_real_issue_tasks(config.tasks_dir, task_ids=config.task_ids)
        experiment_id = (
            f"real-prescreen-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{uuid4().hex[:8]}"
        )
        root = config.output_dir.expanduser().resolve() / experiment_id
        artifacts = root / "artifacts"
        verification = root / "independent-verification"
        summary_path = root / "prescreen-summary.json"
        root.mkdir(parents=True, exist_ok=False)
        started_at = datetime.now(UTC)
        results: list[RealTrialResult] = []
        for task in tasks:
            results.append(
                self._run_trial(
                    config,
                    task,
                    ExperimentArm.TREATMENT,
                    len(results) + 1,
                    1,
                    artifacts,
                    verification,
                )
            )
            summary = self._summary(
                experiment_id, started_at, config, results, summary_path
            )
            summary_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
        return summary

    def _run_trial(
        self,
        config: RealExperimentConfig,
        task: RealIssueTask,
        arm: ExperimentArm,
        sequence: int,
        repetition: int,
        artifacts: Path,
        verification_root: Path,
        *,
        context_enabled: bool | None = None,
        repo_map_enabled: bool | None = None,
    ) -> RealTrialResult:
        """运行一次试验；调用方可显式指定唯一变化的配置开关。"""
        source = (config.source_root / task.id).expanduser().resolve()
        task.validate_checkout(source)
        test_python = _task_python(config.test_env_root, task.id)
        context = ContextConfig.model_validate(
            {
                **config.agent_config.context.model_dump(mode="python"),
                "enabled": (
                    arm is ExperimentArm.TREATMENT
                    if context_enabled is None
                    else context_enabled
                ),
                "compaction_trigger_tokens": config.trigger_tokens,
            }
        )
        repo_map = config.agent_config.repo_map.model_copy(
            update={
                "enabled": (
                    config.agent_config.repo_map.enabled
                    if repo_map_enabled is None
                    else repo_map_enabled
                )
            }
        )
        agent_config = config.agent_config.model_copy(
            update={"context": context, "repo_map": repo_map, "record_request_views": True},
            deep=True,
        )
        run = self.runner.run(
            RunConfig(
                repo=source,
                task=task.problem_statement,
                model_name=config.model_name,
                output_dir=artifacts,
                env_file=config.env_file,
                usd_cny_rate=config.usd_cny_rate,
                llm_timeout_seconds=config.llm_timeout_seconds,
                llm_max_retries=config.llm_max_retries,
                per_request_output_tokens=config.per_request_output_tokens,
                test_python_executable=test_python,
                test_pythonpath_entries=task.test_pythonpath_paths,
                agent_config=agent_config,
            )
        )
        trajectory = analyze_real_trajectory(
            Path(run.trace_path),
            related_files=task.related_context_files,
            target_files=task.expected_source_files,
        )
        changed_tests = _changed_test_files(task, run.changed_files)
        verification = self._independent_verify(
            task,
            source,
            run,
            test_python,
            verification_root / f"{sequence:03d}-{task.id}-{arm.value}",
        )
        agent_completed = run.status is AgentStatus.COMPLETED
        independent_passed = bool(verification and verification.success)
        source_patch_applied = bool(
            verification
            and isinstance(verification.metadata, dict)
            and verification.metadata.get("source_patch_applied") is True
        )
        failures = _eligibility_failures(trajectory, config.trigger_tokens)
        resolved = (
            agent_completed
            and source_patch_applied
            and independent_passed
            and not changed_tests
        )
        return RealTrialResult(
            sequence=sequence,
            task_id=task.id,
            arm=arm,
            repo_map_enabled=repo_map.enabled,
            repetition=repetition,
            run=run,
            agent_completed=agent_completed,
            agent_selected_tests_passed=trajectory.last_agent_test_passed,
            # 这三个真实任务没有提供 Agent 可见、任务专属的公开测试集。test.patch
            # 属于评测端注入的独立测试，故该字段必须为 None，不能偷换概念。
            public_tests_passed=None,
            independent_tests_passed=independent_passed,
            source_patch_applied=source_patch_applied,
            tests_modified=bool(changed_tests),
            changed_test_files=changed_tests,
            resolved=resolved,
            verification=verification,
            trajectory=trajectory,
            eligible_for_paired=not failures,
            eligibility_failures=failures,
        )

    @staticmethod
    def _independent_verify(
        task: RealIssueTask,
        source: Path,
        run: RunResult,
        test_python: Path,
        destination: Path,
    ) -> ToolResult | None:
        """从干净固定提交重建 Agent 补丁，再注入 Agent 看不到的测试。"""
        if not run.diff_path:
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--quiet", "--no-hardlinks", str(source), str(destination)], source.parent)
        patch = Path(run.diff_path)
        if patch.is_file() and patch.stat().st_size:
            try:
                _git(["apply", str(patch)], destination)
            except BenchmarkError:
                return ToolResult(
                    call_id=f"independent-{task.id}",
                    tool_name="run_tests",
                    success=False,
                    error="Agent patch cannot be applied to the pinned base commit",
                    metadata={"source_patch_applied": False, "verification_kind": "hidden"},
                )
        try:
            _git(["apply", str(task.test_patch_path)], destination)
        except BenchmarkError:
            return ToolResult(
                call_id=f"independent-{task.id}",
                tool_name="run_tests",
                success=False,
                error="hidden test patch conflicts with the Agent patch",
                metadata={"source_patch_applied": True, "verification_kind": "hidden"},
            )
        tool = RunTestsTool(
            destination,
            python_executable=test_python,
            pythonpath_entries=task.test_pythonpath_paths,
            default_timeout_seconds=300,
        )
        return tool.execute(
            ToolCall(
                id=f"independent-{task.id}",
                name=tool.spec.name,
                arguments={"command": task.test_command, "timeout_seconds": 300},
            )
        ).model_copy(
            update={
                "metadata": {
                    "verification_kind": "hidden",
                    "source_patch_applied": True,
                }
            }
        )

    @staticmethod
    def _summary(
        experiment_id: str,
        started_at: datetime,
        config: RealExperimentConfig,
        results: list[RealTrialResult],
        summary_path: Path,
    ) -> RealPrescreenSummary:
        eligible = tuple(sorted(result.task_id for result in results if result.eligible_for_paired))
        ineligible = tuple(
            sorted(result.task_id for result in results if not result.eligible_for_paired)
        )
        return RealPrescreenSummary(
            experiment_id=experiment_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            model_name=config.model_name,
            trigger_tokens=config.trigger_tokens,
            results=tuple(results),
            eligible_task_ids=eligible,
            ineligible_task_ids=ineligible,
            eligible_for_formal_experiment=len(eligible) >= 3,
            summary_path=str(summary_path),
        )


class RealPairedExperimentSummary(BaseModel):
    """只有预筛选满足门槛后才允许生成的正式配对实验结果。"""

    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    started_at: datetime
    finished_at: datetime
    repetitions: int
    task_ids: tuple[str, ...]
    trials: tuple[RealTrialResult, ...]
    summary_path: str


class RealPairedExperimentRunner(RealPrescreenRunner):
    """对至少三道已触发折叠的真实任务执行 C/T 交替实验。"""

    def run_paired(
        self,
        config: RealExperimentConfig,
        *,
        eligible_task_ids: tuple[str, ...],
        repetitions: int = 3,
    ) -> RealPairedExperimentSummary:
        """每题每组至少三次；不满足预筛选门槛时拒绝产生无效费用。"""
        if len(set(eligible_task_ids)) < 3:
            raise BenchmarkError("formal real paired experiment requires 3 eligible tasks")
        if repetitions < 3:
            raise BenchmarkError("formal real paired experiment requires at least 3 repetitions")
        tasks = load_real_issue_tasks(config.tasks_dir, task_ids=eligible_task_ids)
        experiment_id = (
            f"real-paired-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{uuid4().hex[:8]}"
        )
        root = config.output_dir.expanduser().resolve() / experiment_id
        artifacts = root / "artifacts"
        verification = root / "independent-verification"
        summary_path = root / "paired-summary.json"
        root.mkdir(parents=True, exist_ok=False)
        started_at = datetime.now(UTC)
        trials: list[RealTrialResult] = []
        for repetition in range(1, repetitions + 1):
            arms = (
                (ExperimentArm.CONTROL, ExperimentArm.TREATMENT)
                if repetition % 2
                else (ExperimentArm.TREATMENT, ExperimentArm.CONTROL)
            )
            for task in tasks:
                for arm in arms:
                    trials.append(
                        self._run_trial(
                            config,
                            task,
                            arm,
                            len(trials) + 1,
                            repetition,
                            artifacts,
                            verification,
                        )
                    )
                    summary = RealPairedExperimentSummary(
                        experiment_id=experiment_id,
                        started_at=started_at,
                        finished_at=datetime.now(UTC),
                        repetitions=repetitions,
                        task_ids=tuple(task.id for task in tasks),
                        trials=tuple(trials),
                        summary_path=str(summary_path),
                    )
                    summary_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
        return summary


class RealRepoMapPrescreenRunner(RealPrescreenRunner):
    """在保持 32k 上下文策略不变时，仅切换 Repo Map 的单次配对预筛选。"""

    def run(self, config: RealExperimentConfig) -> RealRepoMapPrescreenSummary:
        """每题执行 C/T 或 T/C 一次，减少服务时间变化带来的顺序偏差。"""
        tasks = load_real_issue_tasks(config.tasks_dir, task_ids=config.task_ids)
        experiment_id = (
            f"real-repo-map-prescreen-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{uuid4().hex[:8]}"
        )
        root = config.output_dir.expanduser().resolve() / experiment_id
        artifacts, verification = root / "artifacts", root / "independent-verification"
        summary_path = root / "repo-map-prescreen-summary.json"
        root.mkdir(parents=True, exist_ok=False)
        started_at = datetime.now(UTC)
        trials: list[RealTrialResult] = []
        for index, task in enumerate(tasks):
            # 交替首个组别；此处 control/treatment 分别表示禁用/启用 Repo Map。
            arms = (
                (ExperimentArm.CONTROL, ExperimentArm.TREATMENT)
                if index % 2 == 0
                else (ExperimentArm.TREATMENT, ExperimentArm.CONTROL)
            )
            for arm in arms:
                trials.append(
                    self._run_trial(
                        config,
                        task,
                        arm,
                        len(trials) + 1,
                        1,
                        artifacts,
                        verification,
                        context_enabled=config.agent_config.context.enabled,
                        repo_map_enabled=arm is ExperimentArm.TREATMENT,
                    )
                )
                summary = self._summary(experiment_id, started_at, config, trials, summary_path)
                summary_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
        return summary

    @staticmethod
    def _summary(
        experiment_id: str,
        started_at: datetime,
        config: RealExperimentConfig,
        trials: list[RealTrialResult],
        summary_path: Path,
    ) -> RealRepoMapPrescreenSummary:
        """汇总两组成本、验收和首次目标文件读取，不对单次结果做显著性推断。"""
        return RealRepoMapPrescreenSummary(
            experiment_id=experiment_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            model_name=config.model_name,
            trigger_tokens=config.trigger_tokens,
            trials=tuple(trials),
            control=_repo_map_aggregate(trials, ExperimentArm.CONTROL),
            treatment=_repo_map_aggregate(trials, ExperimentArm.TREATMENT),
            summary_path=str(summary_path),
        )


def analyze_real_trajectory(
    path: Path,
    *,
    related_files: tuple[str, ...],
    target_files: tuple[str, ...] = (),
) -> RealTrajectoryMetrics:
    """从脱敏轨迹中计算预筛选指标，不读取模型隐藏思考。"""
    base = analyze_trajectory(path)
    before = after = patch_calls = test_calls = request_views = 0
    reads: set[str] = set()
    targets = {Path(value).as_posix() for value in target_files}
    first_target_step: int | None = None
    first_target_call: int | None = None
    first_target_path: str | None = None
    first_patch_step: int | None = None
    first_test_step: int | None = None
    post_target_searches = 0
    cached_calls = 0
    phase_transitions: list[str] = []
    tool_call_number = 0
    last_test: bool | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        payload = event.get("payload", {})
        if event.get("event_type") == "context_prepared":
            before = max(before, int(payload.get("estimated_tokens_before", 0) or 0))
            after = max(after, int(payload.get("estimated_tokens_after", 0) or 0))
        elif event.get("event_type") == "model_request_view":
            request_views += 1
        elif event.get("event_type") == "tool_called":
            tool_call_number += 1
            call = payload.get("call", {})
            name = call.get("name")
            if name == "read_file":
                value = call.get("arguments", {}).get("path")
                if isinstance(value, str):
                    normalized = Path(value).as_posix()
                    reads.add(normalized)
                    if normalized in targets and first_target_path is None:
                        first_target_step = int(event.get("step", 0) or 0)
                        first_target_call = tool_call_number
                        first_target_path = normalized
            elif name == "search_code" and first_target_path is not None:
                post_target_searches += 1
            elif name == "apply_patch":
                patch_calls += 1
                if first_patch_step is None:
                    first_patch_step = int(event.get("step", 0) or 0)
            elif name == "run_tests":
                test_calls += 1
                if first_test_step is None:
                    first_test_step = int(event.get("step", 0) or 0)
        elif event.get("event_type") == "tool_returned":
            result = payload.get("result", {})
            metadata = result.get("metadata", {})
            if isinstance(metadata, dict) and metadata.get("cached") is True:
                cached_calls += 1
            if result.get("tool_name") == "run_tests":
                last_test = bool(result.get("success"))
        elif event.get("event_type") == "agent_phase_changed":
            source, destination = payload.get("from"), payload.get("to")
            if isinstance(source, str) and isinstance(destination, str):
                phase_transitions.append(f"{source}->{destination}")
    related = set(related_files)
    return RealTrajectoryMetrics(
        behavior=base,
        max_estimated_tokens_before=before,
        max_estimated_tokens_after=after,
        unique_read_files=tuple(sorted(reads)),
        meaningful_read_files=tuple(sorted(reads.intersection(related))),
        apply_patch_calls=patch_calls,
        run_tests_calls=test_calls,
        last_agent_test_passed=last_test,
        request_views_recorded=request_views,
        first_target_read_step=first_target_step,
        first_target_read_tool_call=first_target_call,
        first_target_read_path=first_target_path,
        first_patch_step=first_patch_step,
        first_test_step=first_test_step,
        post_target_search_calls=post_target_searches,
        cached_tool_calls=cached_calls,
        phase_transitions=tuple(phase_transitions),
    )


def _repo_map_aggregate(
    trials: list[RealTrialResult], arm: ExperimentArm
) -> RepoMapArmAggregate:
    """在不丢失费用完整性语义的前提下聚合单次 Repo Map 预筛选。"""
    selected = [trial for trial in trials if trial.arm is arm]
    first_steps = [
        trial.trajectory.first_target_read_step
        for trial in selected
        if trial.trajectory.first_target_read_step is not None
    ]
    cost_complete = all(trial.run.cost_complete for trial in selected)
    return RepoMapArmAggregate(
        trial_count=len(selected),
        agent_completed_count=sum(trial.agent_completed for trial in selected),
        public_tests_passed_count=sum(trial.public_tests_passed is True for trial in selected),
        independent_tests_passed_count=sum(trial.independent_tests_passed for trial in selected),
        resolved_count=sum(trial.resolved for trial in selected),
        tests_modified_count=sum(trial.tests_modified for trial in selected),
        input_tokens=sum(trial.run.input_tokens for trial in selected),
        output_tokens=sum(trial.run.output_tokens for trial in selected),
        cost_usd=sum(trial.run.cost_usd for trial in selected),
        cost_cny_estimate=(
            sum(trial.run.cost_cny_estimate or 0 for trial in selected)
            if cost_complete
            else None
        ),
        target_read_count=len(first_steps),
        average_first_target_read_step=(
            sum(first_steps) / len(first_steps) if first_steps else None
        ),
    )


def _eligibility_failures(
    metrics: RealTrajectoryMetrics, trigger_tokens: int
) -> tuple[str, ...]:
    """按实验计划的四项门槛返回明确的未入选原因。"""
    failures: list[str] = []
    if metrics.behavior.compaction_count < 1:
        failures.append("no_history_compaction")
    if metrics.max_estimated_tokens_before < trigger_tokens:
        failures.append("request_never_reached_trigger")
    if len(metrics.meaningful_read_files) < 5:
        failures.append("fewer_than_five_meaningful_files_read")
    if metrics.run_tests_calls < 1 or metrics.apply_patch_calls < 1:
        failures.append("no_test_and_patch_feedback_loop")
    if metrics.request_views_recorded < 1:
        failures.append("request_views_missing")
    return tuple(failures)


def _changed_test_files(task: RealIssueTask, files: tuple[str, ...]) -> tuple[str, ...]:
    """识别常见测试目录及任务明确声明的隐藏测试文件。"""
    expected = set(task.expected_test_files)
    return tuple(
        sorted(
            path
            for path in files
            if Path(path).as_posix() in expected
            or Path(path).as_posix().startswith(("tests/", "testing/"))
            or Path(path).name.startswith("test_")
        )
    )


def _task_python(root: Path, task_id: str) -> Path:
    """兼容 Windows 与 POSIX 虚拟环境布局，并拒绝静默回退。"""
    environment = root.expanduser().resolve() / task_id
    for candidate in (
        environment / "Scripts" / "python.exe",
        environment / "bin" / "python",
    ):
        if candidate.is_file():
            return candidate
    raise BenchmarkError(
        "real task test interpreter does not exist",
        context={"task_id": task_id, "environment_name": environment.name},
    )


def _returncode(result: ToolResult) -> int | None:
    """从强类型工具结果中安全提取 pytest 退出码。"""
    if not isinstance(result.output, dict):
        return None
    value = result.output.get("returncode")
    return value if isinstance(value, int) else None


def _git(arguments: list[str], cwd: Path) -> None:
    """执行实验器内部的固定 Git 操作，并统一保留可诊断错误。"""
    try:
        result = subprocess.run(
            # 真实任务会经历多层实验目录和 clone；在 Windows CI 中需要为每条
            # Git 命令显式开启长路径，同时避免改写宿主机的全局 Git 配置。
            ["git", "-c", "core.longpaths=true", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BenchmarkError(f"real experiment git command failed: {exc}") from exc
    if result.returncode != 0:
        raise BenchmarkError(
            "real experiment git command failed",
            context={
                "arguments": arguments,
                "returncode": result.returncode,
                "stderr": result.stderr.strip(),
            },
        )
