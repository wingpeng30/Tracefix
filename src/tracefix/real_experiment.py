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


class RealTrialResult(BaseModel):
    """一次真实 Issue 运行及 Agent 不可见的独立验收结果。"""

    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    task_id: str
    arm: ExperimentArm
    repetition: int = Field(default=1, ge=1)
    run: RunResult
    agent_completed: bool
    public_tests_passed: bool | None = None
    independent_tests_passed: bool
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
            max_input_tokens=240_000,
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
    ) -> RealTrialResult:
        """保持其他参数不变，仅按 arm 切换上下文压缩。"""
        source = (config.source_root / task.id).expanduser().resolve()
        task.validate_checkout(source)
        test_python = _task_python(config.test_env_root, task.id)
        context = ContextConfig.model_validate(
            {
                **config.agent_config.context.model_dump(mode="python"),
                "enabled": arm is ExperimentArm.TREATMENT,
                "compaction_trigger_tokens": config.trigger_tokens,
            }
        )
        agent_config = config.agent_config.model_copy(
            update={"context": context, "record_request_views": True}, deep=True
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
            Path(run.trace_path), related_files=task.related_context_files
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
        failures = _eligibility_failures(trajectory, config.trigger_tokens)
        resolved = agent_completed and independent_passed and not changed_tests
        return RealTrialResult(
            sequence=sequence,
            task_id=task.id,
            arm=arm,
            repetition=repetition,
            run=run,
            agent_completed=agent_completed,
            public_tests_passed=trajectory.last_agent_test_passed,
            independent_tests_passed=independent_passed,
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
                )
        try:
            _git(["apply", str(task.test_patch_path)], destination)
        except BenchmarkError:
            return ToolResult(
                call_id=f"independent-{task.id}",
                tool_name="run_tests",
                success=False,
                error="hidden test patch conflicts with the Agent patch",
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
        ).model_copy(update={"metadata": {"verification_kind": "hidden"}})

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


def analyze_real_trajectory(
    path: Path,
    *,
    related_files: tuple[str, ...],
) -> RealTrajectoryMetrics:
    """从脱敏轨迹中计算预筛选指标，不读取模型隐藏思考。"""
    base = analyze_trajectory(path)
    before = after = patch_calls = test_calls = request_views = 0
    reads: set[str] = set()
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
            call = payload.get("call", {})
            name = call.get("name")
            if name == "read_file":
                value = call.get("arguments", {}).get("path")
                if isinstance(value, str):
                    reads.add(Path(value).as_posix())
            elif name == "apply_patch":
                patch_calls += 1
            elif name == "run_tests":
                test_calls += 1
        elif event.get("event_type") == "tool_returned":
            result = payload.get("result", {})
            if result.get("tool_name") == "run_tests":
                last_test = bool(result.get("success"))
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
