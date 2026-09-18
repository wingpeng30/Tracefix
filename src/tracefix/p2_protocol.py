"""P2 真实任务 C/T 实验的冻结协议与零费用演练。"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracefix.agent import AgentConfig
from tracefix.context import ContextConfig
from tracefix.exceptions import BenchmarkError
from tracefix.messages import Message, MessageRole, ToolCall
from tracefix.models import BaseLLM, LLMConfig, LLMResponse, TokenUsage
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.paired import ExperimentArm
from tracefix.provenance import inspect_test_environment
from tracefix.real_benchmark import RealIssueTask, load_real_issue_tasks
from tracefix.real_environment import resolve_managed_environment_python
from tracefix.real_experiment import validate_agent_patch_strict
from tracefix.real_recipes import load_environment_recipes
from tracefix.repository import RepoMapConfig
from tracefix.runtime import RunConfig, TraceFixRunner
from tracefix.tools.base import ToolSpec

P1_QUALIFIED_TASK_IDS = (
    "psf__requests-1142", "psf__requests-1766", "pylint-dev__pylint-4551",
    "pylint-dev__pylint-4604", "pylint-dev__pylint-4661", "pytest-dev__pytest-10051",
    "pytest-dev__pytest-10081", "pytest-dev__pytest-10356", "sphinx-doc__sphinx-10435",
    "sphinx-doc__sphinx-10449",
)
COLLECTION_FAILURE_TASK_IDS = ("pylint-dev__pylint-4551", "pylint-dev__pylint-4604")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    if completed.returncode:
        raise BenchmarkError("cannot determine P2 code commit")
    return completed.stdout.strip()


def _tracked_diff(root: Path) -> bytes:
    completed = subprocess.run(
        ["git", "diff", "--binary", "HEAD"], cwd=root, capture_output=True, check=False
    )
    if completed.returncode:
        raise BenchmarkError("cannot inspect P2 tracked worktree")
    return completed.stdout


def _code_hashes(root: Path) -> dict[str, str]:
    source = root.expanduser().resolve() / "src" / "tracefix"
    if not source.is_dir():
        return {}
    return {
        path.relative_to(root.resolve()).as_posix(): _sha256(path)
        for path in sorted(source.rglob("*.py"))
    }


class P2FormalRunRequirements(BaseModel):
    """正式调用前必须由操作者填写且可审计的商业参数。"""

    model_config = ConfigDict(extra="forbid")

    model_name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    pricing_source: str = Field(min_length=1)
    total_cost_cap_usd: float = Field(gt=0)
    input_cost_per_million_usd: float = Field(gt=0)
    output_cost_per_million_usd: float = Field(gt=0)


class P2ProtocolConfig(BaseModel):
    """固定整体优化 C/T 设计；离线模式从不创建供应商客户端。"""

    model_config = ConfigDict(extra="forbid")

    tasks_dir: Path = Path("benchmarks/real_candidates")
    recipes_dir: Path = Path("benchmarks/real_recipes")
    source_root: Path = Path("runs/real-candidate-validation-v080b")
    test_env_root: Path = Path("runs/p1-revalidation-20260917/environments")
    output_dir: Path = Path("runs")
    repetitions: int = Field(default=3, ge=3, le=3)
    context_trigger_tokens: int = Field(default=32_000, ge=1)
    max_input_tokens: int = Field(default=350_000, ge=1)
    max_output_tokens: int = Field(default=20_000, ge=1)
    max_steps: int = Field(default=24, ge=1)
    max_test_runs: int = Field(default=6, ge=1)
    wall_time_seconds: int = Field(default=900, ge=1)
    per_request_output_tokens: int = Field(default=4_096, ge=1)
    formal: P2FormalRunRequirements | None = None

    @model_validator(mode="after")
    def validate_design(self) -> P2ProtocolConfig:
        if self.repetitions != 3:
            raise ValueError("P2 fixes exactly three repetitions per arm")
        return self


class P2TrialPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    task_id: str
    repetition: int = Field(ge=1, le=3)
    arm: ExperimentArm
    token_optimization_enabled: bool
    repo_map_enabled: bool
    context_compaction_enabled: bool


class P2ProtocolRecord(BaseModel):
    """可提交的脱敏协议，包含可重建运行的全部身份信息。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = "p2_whole_system_ct_protocol"
    generated_at: datetime
    code_commit: str
    offline_only: bool
    formal_ready: bool
    formal_missing: tuple[str, ...]
    qualified_task_ids: tuple[str, ...]
    primary_task_ids: tuple[str, ...]
    collection_failure_task_ids: tuple[str, ...]
    task_hashes: dict[str, dict[str, str]]
    recipe_hashes: dict[str, str]
    code_hashes: dict[str, str] = Field(default_factory=dict)
    budgets: dict[str, int]
    arm_configurations: dict[str, dict[str, bool | int]]
    schedule: tuple[P2TrialPlan, ...]
    formal: P2FormalRunRequirements | None = None


class P2TrialRecord(BaseModel):
    """一项可恢复试次；模拟与正式模式写入同一结构。"""

    model_config = ConfigDict(extra="forbid")

    sequence: int
    task_id: str
    arm: ExperimentArm
    repetition: int
    mode: str
    status: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    stop_reason: str | None = None
    independent_passed: bool | None = None
    resumed: bool = False
    run_result_path: str | None = None
    verification_eligible: bool | None = None


class P2InputCheck(BaseModel):
    """冻结运行前对每题源码、配方和受管解释器的可审计检查。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    source: str
    source_commit: str
    source_clean: bool
    recipe_fingerprint: str
    test_python: str
    dependency_fingerprint: str


class P2CheckRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = "p2_input_check"
    generated_at: datetime
    code_commit: str
    tracked_worktree_dirty: bool
    tracked_diff_sha256: str
    protocol: P2ProtocolRecord
    inputs: tuple[P2InputCheck, ...]


class P2SimulationLLM(BaseLLM):
    """只写无答案标记文件的确定性模型，用于验证真实 Agent 工具闭环。"""

    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        self.calls = 0

    def complete(self, messages, tools=()) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            message = Message(
                role=MessageRole.ASSISTANT,
                tool_calls=(ToolCall(
                    id="simulation-marker", name="apply_patch",
                    arguments={"patch": (
                        "*** Begin Patch\n*** Add File: tracefix_simulation_note.txt\n"
                        "+P2 engineering simulation; no task answer.\n*** End Patch"
                    )},
                ),),
            )
        else:
            message = Message(role=MessageRole.ASSISTANT, content="simulation complete")
        return LLMResponse(
            message=message,
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2, cost_usd=0),
            model_name=self.config.model_name,
        )


class P2RunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str
    protocol_path: str
    trial_count: int
    completed_count: int
    resumed_count: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    results: tuple[P2TrialRecord, ...]
    engineering_simulation_only: bool
    summary_path: str


class P2CostLedgerRecord(BaseModel):
    """正式实验的持久化费用状态；不确定请求会冻结后续调用。"""

    model_config = ConfigDict(extra="forbid")

    cap_usd: float
    spent_usd: float = 0
    reserved_usd: float = 0
    request_count: int = 0
    uncertain_request: bool = False


class P2BudgetedLLM(BaseLLM):
    """在实际供应商调用边界进行费用预留和核算。"""

    def __init__(self, config: LLMConfig, *, ledger_path: Path,
                 formal: P2FormalRunRequirements, input_upper_bound: int) -> None:
        super().__init__(config)
        self._delegate = LiteLLMAdapter(config.model_copy(update={"max_retries": 0}))
        self._ledger_path = ledger_path
        self._formal = formal
        self._input_upper_bound = input_upper_bound

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec] = ()) -> LLMResponse:
        ledger = _read_cost_ledger(self._ledger_path, self._formal.total_cost_cap_usd)
        if ledger.uncertain_request:
            raise BenchmarkError("P2 cost ledger contains an uncertain request")
        reservation = estimated_request_reservation(
            self._formal, input_tokens=self._input_upper_bound,
            output_tokens=self.config.max_output_tokens or 0,
        )
        if ledger.spent_usd + ledger.reserved_usd + reservation > ledger.cap_usd:
            raise BenchmarkError("P2 total cost cap would be exceeded")
        ledger = ledger.model_copy(update={
            "reserved_usd": ledger.reserved_usd + reservation,
            "request_count": ledger.request_count + 1,
            "uncertain_request": True,
        })
        _write_cost_ledger(self._ledger_path, ledger)
        response = self._delegate.complete(messages, tools)
        usage = response.usage
        actual = estimated_request_reservation(
            self._formal, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        )
        ledger = ledger.model_copy(update={
            "spent_usd": ledger.spent_usd + actual,
            "reserved_usd": max(0, ledger.reserved_usd - reservation),
            "uncertain_request": False,
        })
        _write_cost_ledger(self._ledger_path, ledger)
        return response.model_copy(update={"usage": usage.model_copy(update={"cost_usd": actual})})


def estimated_request_reservation(formal: P2FormalRunRequirements, *, input_tokens: int,
                                  output_tokens: int) -> float:
    """在供应商调用前按每次上限保留费用，避免超过总帽。"""
    return (
        input_tokens * formal.input_cost_per_million_usd
        + output_tokens * formal.output_cost_per_million_usd
    ) / 1_000_000


def _write_cost_ledger(path: Path, ledger: P2CostLedgerRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(ledger.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _read_cost_ledger(path: Path, cap_usd: float) -> P2CostLedgerRecord:
    if not path.is_file():
        return P2CostLedgerRecord(cap_usd=cap_usd)
    ledger = P2CostLedgerRecord.model_validate_json(path.read_text(encoding="utf-8"))
    if ledger.cap_usd != cap_usd:
        raise BenchmarkError("P2 cost cap does not match the persisted ledger")
    return ledger


def write_trial_record(path: Path, record: P2TrialRecord) -> None:
    """原子更新单独试次记录，崩溃时保留上一份完整 JSON。"""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_completed_trial(path: Path) -> P2TrialRecord | None:
    """仅复用明确完成的试次；未知或部分状态必须停止人工处理。"""
    if not path.is_file():
        return None
    record = P2TrialRecord.model_validate_json(path.read_text(encoding="utf-8"))
    if record.status == "verification_complete":
        return record.model_copy(update={"resumed": True})
    raise BenchmarkError(
        "P2 trial has uncertain prior state; do not resend request",
        context={"path": str(path)},
    )


def _task_hashes(tasks: tuple[RealIssueTask, ...]) -> dict[str, dict[str, str]]:
    return {
        task.id: {
            "base_commit": task.base_commit,
            "problem_statement": task.hashes.problem_statement,
            "gold_patch": task.hashes.gold_patch,
            "test_patch": task.hashes.test_patch,
        }
        for task in tasks
    }


def _schedule(task_ids: tuple[str, ...]) -> tuple[P2TrialPlan, ...]:
    plans: list[P2TrialPlan] = []
    for repetition, arms in enumerate(
        ((ExperimentArm.CONTROL, ExperimentArm.TREATMENT),
         (ExperimentArm.TREATMENT, ExperimentArm.CONTROL),
         (ExperimentArm.CONTROL, ExperimentArm.TREATMENT)), start=1
    ):
        for task_id in task_ids:
            for arm in arms:
                enabled = arm is ExperimentArm.TREATMENT
                plans.append(P2TrialPlan(
                    sequence=len(plans) + 1, task_id=task_id, repetition=repetition, arm=arm,
                    token_optimization_enabled=enabled, repo_map_enabled=enabled,
                    context_compaction_enabled=enabled,
                ))
    return tuple(plans)


def build_p2_protocol(
    config: P2ProtocolConfig, *, repository_root: Path = Path(".")
) -> P2ProtocolRecord:
    """验证 P1 入选集，冻结输入，并构造唯一的 60 次试验顺序。"""
    tasks = load_real_issue_tasks(config.tasks_dir, task_ids=P1_QUALIFIED_TASK_IDS)
    ids = tuple(task.id for task in tasks)
    if ids != tuple(sorted(P1_QUALIFIED_TASK_IDS)):
        raise BenchmarkError("P2 qualified task set is incomplete or changed")
    recipes = load_environment_recipes(config.recipes_dir)
    missing_recipes = sorted(set(ids) - set(recipes))
    if missing_recipes:
        raise BenchmarkError(
            "P2 is missing environment recipes", context={"task_ids": missing_recipes}
        )
    missing = (
        ()
        if config.formal
        else ("model_name", "provider", "pricing_source", "total_cost_cap_usd")
    )
    return P2ProtocolRecord(
        generated_at=datetime.now(UTC),
        code_commit=_git_commit(repository_root),
        offline_only=config.formal is None,
        formal_ready=config.formal is not None,
        formal_missing=missing,
        qualified_task_ids=ids,
        primary_task_ids=tuple(item for item in ids if item not in COLLECTION_FAILURE_TASK_IDS),
        collection_failure_task_ids=COLLECTION_FAILURE_TASK_IDS,
        task_hashes=_task_hashes(tasks),
        recipe_hashes={key: recipes[key].fingerprint for key in ids},
        code_hashes=_code_hashes(repository_root),
        budgets={
            "max_input_tokens": config.max_input_tokens,
            "max_output_tokens": config.max_output_tokens,
            "max_steps": config.max_steps,
            "max_test_runs": config.max_test_runs,
            "wall_time_seconds": config.wall_time_seconds,
            "per_request_output_tokens": config.per_request_output_tokens,
        },
        arm_configurations={
            "control": {
                "token_optimization_enabled": False,
                "repo_map_enabled": False,
                "context_compaction_enabled": False,
                "context_trigger_tokens": config.context_trigger_tokens,
            },
            "treatment": {
                "token_optimization_enabled": True,
                "repo_map_enabled": True,
                "context_compaction_enabled": True,
                "context_trigger_tokens": config.context_trigger_tokens,
            },
        },
        schedule=_schedule(ids),
        formal=config.formal,
    )


def write_p2_dry_run(config: P2ProtocolConfig, *, repository_root: Path = Path(".")) -> Path:
    """落盘零费用演练记录；此函数不初始化 LLM 或读取任何密钥。"""
    protocol = build_p2_protocol(config, repository_root=repository_root)
    root = config.output_dir.expanduser().resolve() / "p2-dry-run"
    root.mkdir(parents=True, exist_ok=False)
    path = root / "p2-protocol.json"
    path.write_text(protocol.model_dump_json(indent=2), encoding="utf-8")
    return path


def check_p2_inputs(
    config: P2ProtocolConfig, *, repository_root: Path = Path(".")
) -> P2CheckRecord:
    """拒绝脏源码、错误提交、缺失配方或不健康的受管环境。"""
    protocol = build_p2_protocol(config, repository_root=repository_root)
    tasks = load_real_issue_tasks(config.tasks_dir, task_ids=protocol.qualified_task_ids)
    recipes = load_environment_recipes(config.recipes_dir)
    inputs: list[P2InputCheck] = []
    for task in tasks:
        source = (config.source_root / task.id).expanduser().resolve()
        task.validate_checkout(source)
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=source, capture_output=True,
            text=True, check=False,
        )
        if dirty.returncode or dirty.stdout.strip():
            raise BenchmarkError("P2 source checkout is not clean", context={"task_id": task.id})
        python = resolve_managed_environment_python(config.test_env_root, task.id)
        provenance = inspect_test_environment(
            python, pythonpath_entries=task.test_pythonpath_paths
        )
        inputs.append(P2InputCheck(
            task_id=task.id, source=str(source), source_commit=task.base_commit,
            source_clean=True, recipe_fingerprint=recipes[task.id].fingerprint,
            test_python=str(python), dependency_fingerprint=provenance.fingerprint_sha256,
        ))
    tracked_diff = _tracked_diff(repository_root)
    if config.formal is not None and tracked_diff:
        raise BenchmarkError("formal P2 run requires a clean tracked worktree")
    return P2CheckRecord(
        generated_at=datetime.now(UTC), code_commit=_git_commit(repository_root),
        tracked_worktree_dirty=bool(tracked_diff),
        tracked_diff_sha256=hashlib.sha256(tracked_diff).hexdigest(),
        protocol=protocol, inputs=tuple(inputs),
    )


def write_p2_check(config: P2ProtocolConfig, *, repository_root: Path = Path(".")) -> Path:
    """保存 P2 输入冻结检查；任何不匹配都会在写入前中止。"""
    record = check_p2_inputs(config, repository_root=repository_root)
    root = config.output_dir.expanduser().resolve() / "p2-check"
    root.mkdir(parents=True, exist_ok=False)
    path = root / "p2-input-check.json"
    path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    return path


def run_p2_experiment(
    config: P2ProtocolConfig, *, experiment_dir: Path, mode: str,
    repository_root: Path = Path(".")
) -> P2RunSummary:
    """执行并恢复统一 P2 流程；仅 formal 模式创建供应商客户端。"""
    if mode not in {"simulation", "formal"}:
        raise BenchmarkError("unknown P2 execution mode", context={"mode": mode})
    if mode == "formal" and config.formal is None:
        raise BenchmarkError("formal P2 run requires complete commercial parameters")
    check = check_p2_inputs(config, repository_root=repository_root)
    protocol = check.protocol
    root = experiment_dir.expanduser().resolve()
    trials_root = root / "trials"
    trials_root.mkdir(parents=True, exist_ok=True)
    protocol_path = root / "protocol.json"
    if protocol_path.exists():
        persisted = P2ProtocolRecord.model_validate_json(
            protocol_path.read_text(encoding="utf-8")
        )
        comparable = protocol.model_copy(update={"generated_at": persisted.generated_at})
        if comparable != persisted:
            raise BenchmarkError("P2 resume protocol identity does not match")
    else:
        protocol_path.write_text(protocol.model_dump_json(indent=2), encoding="utf-8")
    results: list[P2TrialRecord] = []
    tasks = {
        item.id: item
        for item in load_real_issue_tasks(
            config.tasks_dir, task_ids=protocol.qualified_task_ids
        )
    }
    recipes = load_environment_recipes(config.recipes_dir)
    if mode == "simulation":
        runner = TraceFixRunner(P2SimulationLLM)
        model_name = "tracefix/p2-simulation"
    else:
        assert config.formal is not None
        ledger_path = root / "cost-ledger.json"
        formal = config.formal

        def formal_factory(llm_config: LLMConfig) -> BaseLLM:
            return P2BudgetedLLM(
                llm_config, ledger_path=ledger_path, formal=formal,
                input_upper_bound=config.max_input_tokens,
            )

        runner = TraceFixRunner(formal_factory)
        model_name = formal.model_name
    lock_path = root / ".p2-run.lock"
    try:
        lock_handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise BenchmarkError(
            "P2 experiment is already running or needs manual recovery",
            context={"lock": str(lock_path)},
        ) from exc
    os.write(lock_handle, str(os.getpid()).encode())
    os.close(lock_handle)
    try:
      for plan in protocol.schedule:
        path = trials_root / f"{plan.sequence:03d}.json"
        existing = read_completed_trial(path)
        if existing:
            results.append(existing)
            continue
        task = tasks[plan.task_id]
        enabled = plan.arm is ExperimentArm.TREATMENT
        agent_config = AgentConfig(
            max_steps=config.max_steps, max_input_tokens=config.max_input_tokens,
            max_output_tokens=config.max_output_tokens, wall_time_seconds=config.wall_time_seconds,
            max_test_runs=config.max_test_runs, token_optimization_enabled=enabled,
            context=ContextConfig(
                enabled=enabled, compaction_trigger_tokens=config.context_trigger_tokens
            ),
            repo_map=RepoMapConfig(enabled=enabled), record_request_views=True,
        )
        result = runner.run(RunConfig(
            repo=config.source_root / task.id, task=task.problem_statement,
            model_name=model_name, output_dir=root / "agent-runs",
            env_file=None, llm_max_retries=0,
            per_request_output_tokens=config.per_request_output_tokens,
            test_python_executable=resolve_managed_environment_python(
                config.test_env_root, task.id
            ),
            test_pythonpath_entries=task.test_pythonpath_paths, agent_config=agent_config,
        ))
        if mode == "formal" and config.formal is not None:
            ledger = _read_cost_ledger(root / "cost-ledger.json", config.formal.total_cost_cap_usd)
            if ledger.uncertain_request:
                record = P2TrialRecord(
                    sequence=plan.sequence, task_id=plan.task_id, arm=plan.arm,
                    repetition=plan.repetition, mode=mode, status="request_uncertain",
                    input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                    cost_usd=result.cost_usd, stop_reason=result.stop_reason,
                    run_result_path=result.result_path,
                )
                write_trial_record(path, record)
                raise BenchmarkError("P2 provider request state is uncertain; formal run stopped")
        if not result.diff_path:
            record = P2TrialRecord(
                sequence=plan.sequence, task_id=plan.task_id, arm=plan.arm,
                repetition=plan.repetition, mode=mode, status="infrastructure_error",
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                cost_usd=result.cost_usd, stop_reason=result.stop_reason,
                run_result_path=result.result_path,
            )
            write_trial_record(path, record)
            results.append(record)
            continue
        verification = validate_agent_patch_strict(
            task, source=config.source_root / task.id, agent_patch=Path(result.diff_path),
            test_python=resolve_managed_environment_python(config.test_env_root, task.id),
            output_dir=root / "verification" / f"{plan.sequence:03d}",
            recipe=recipes[task.id],
        )
        record = P2TrialRecord(
            sequence=plan.sequence, task_id=plan.task_id, arm=plan.arm,
            repetition=plan.repetition, mode=mode, status="verification_complete",
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            cost_usd=result.cost_usd, stop_reason=result.stop_reason,
            independent_passed=verification.eligible,
            verification_eligible=verification.eligible,
            run_result_path=result.result_path,
        )
        write_trial_record(path, record)
        results.append(record)
    finally:
        if lock_path.exists():
            lock_path.unlink()
    summary_path = root / "summary.json"
    summary = P2RunSummary(
        mode=mode, protocol_path=str(protocol_path), trial_count=len(results),
        completed_count=sum(item.status == "verification_complete" for item in results),
        resumed_count=sum(item.resumed for item in results),
        input_tokens=sum(item.input_tokens for item in results),
        output_tokens=sum(item.output_tokens for item in results),
        cost_usd=sum(item.cost_usd or 0 for item in results), results=tuple(results),
        engineering_simulation_only=mode == "simulation", summary_path=str(summary_path),
    )
    temporary = summary_path.with_suffix(".tmp")
    temporary.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary, summary_path)
    return summary


def run_p2_simulation(
    config: P2ProtocolConfig, *, experiment_dir: Path, repository_root: Path = Path(".")
) -> P2RunSummary:
    return run_p2_experiment(
        config, experiment_dir=experiment_dir, mode="simulation",
        repository_root=repository_root,
    )


def run_p2_formal(
    config: P2ProtocolConfig, *, experiment_dir: Path, repository_root: Path = Path(".")
) -> P2RunSummary:
    return run_p2_experiment(
        config, experiment_dir=experiment_dir, mode="formal",
        repository_root=repository_root,
    )
