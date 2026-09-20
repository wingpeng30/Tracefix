"""P2 真实任务 C/T 实验的冻结协议与零费用演练。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

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
    "psf__requests-1142",
    "psf__requests-1766",
    "pylint-dev__pylint-4551",
    "pylint-dev__pylint-4604",
    "pylint-dev__pylint-4661",
    "pytest-dev__pytest-10051",
    "pytest-dev__pytest-10081",
    "pytest-dev__pytest-10356",
    "sphinx-doc__sphinx-10435",
    "sphinx-doc__sphinx-10449",
)
COLLECTION_FAILURE_TASK_IDS = ("pylint-dev__pylint-4551", "pylint-dev__pylint-4604")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_sha256(path: str | None) -> str | None:
    candidate = Path(path) if path else None
    return _sha256(candidate) if candidate and candidate.is_file() else None


def _verify_saved_artifacts(record: P2TrialRecord) -> None:
    """新格式记录恢复前逐件核对；旧证据仅可阅读，不能伪装为新格式。"""
    for location, digest, label in (
        (record.run_result_path, record.run_result_sha256, "run result"),
        (record.agent_patch_path, record.agent_patch_sha256, "agent patch"),
        (record.verification_path, record.verification_sha256, "verification"),
    ):
        if digest is not None and _artifact_sha256(location) != digest:
            raise BenchmarkError(
                "P2 persisted artifact identity does not match", context={"artifact": label}
            )


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
    currency: str = "USD"
    input_cache_hit_cost_per_million: float | None = Field(default=None, gt=0)
    input_cache_miss_cost_per_million: float | None = Field(default=None, gt=0)
    output_cost_per_million: float | None = Field(default=None, gt=0)
    peak_pricing: bool = True

    @model_validator(mode="after")
    def validate_currency_pricing(self) -> P2FormalRunRequirements:
        if self.currency == "CNY":
            if None in (
                self.input_cache_hit_cost_per_million,
                self.input_cache_miss_cost_per_million,
                self.output_cost_per_million,
            ):
                raise ValueError(
                    "CNY formal pricing requires cache-hit, cache-miss, and output prices"
                )
        elif self.currency != "USD":
            raise ValueError("P2 formal currency must be USD or CNY")
        return self

    @property
    def cap(self) -> float:
        return self.total_cost_cap_usd

    @property
    def conservative_input_price(self) -> float:
        return self.input_cache_miss_cost_per_million or self.input_cost_per_million_usd

    @property
    def effective_output_price(self) -> float:
        return self.output_cost_per_million or self.output_cost_per_million_usd


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
    per_request_input_tokens: int = Field(default=128_000, ge=1)
    per_request_output_tokens: int = Field(default=4_096, ge=1)
    # Command-line P2 entry points supply the retained P1 record.  Keeping this
    # explicit prevents a library caller from accidentally treating an arbitrary
    # ten-task list as the P1-qualified pool.
    p1_evidence_path: Path | None = None
    formal: P2FormalRunRequirements | None = None


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
    p1_evidence_sha256: str | None = None
    p1_qualifications: dict[str, P2QualificationRecord] = Field(default_factory=dict)
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
    attempt_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    stop_reason: str | None = None
    independent_passed: bool | None = None
    resumed: bool = False
    run_result_path: str | None = None
    agent_patch_path: str | None = None
    verification_eligible: bool | None = None
    verification_path: str | None = None
    agent_duration_seconds: float | None = Field(default=None, ge=0)
    verification_duration_seconds: float | None = Field(default=None, ge=0)
    run_result_sha256: str | None = None
    agent_patch_sha256: str | None = None
    verification_sha256: str | None = None


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


class P2QualificationRecord(BaseModel):
    """从 P1 原始记录冻结的资格身份与完整通过集合。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    qualification_type: str
    base_commit: str
    dependency_fingerprint: str
    expected_node_ids: tuple[str, ...]


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
                tool_calls=(
                    ToolCall(
                        id="simulation-marker",
                        name="apply_patch",
                        arguments={
                            "patch": (
                                "diff --git a/tracefix_simulation_note.txt "
                                "b/tracefix_simulation_note.txt\n"
                                "new file mode 100644\n--- /dev/null\n"
                                "+++ b/tracefix_simulation_note.txt\n@@ -0,0 +1 @@\n"
                                "+P2 engineering simulation; no task answer.\n"
                            )
                        },
                    ),
                ),
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
    halt_reason: str | None = None
    protocol_identity: str | None = None
    provider: str | None = None
    model_name: str | None = None
    currency: str = "USD"
    requests: tuple[P2CostRequestRecord, ...] = ()


class P2CostRequestRecord(BaseModel):
    """单次供应商请求的不可覆盖账本条目。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    status: str
    reserved_usd: float
    input_token_upper_bound: int
    output_token_upper_bound: int
    actual_input_tokens: int | None = None
    actual_output_tokens: int | None = None
    actual_cost_usd: float | None = None
    actual_cost_source: str | None = None
    input_cache_hit_tokens: int | None = None
    input_cache_miss_tokens: int | None = None


class P2TaskSummary(BaseModel):
    """一个任务三次 C/T 的完整、可比较汇总。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    qualification_type: str
    planned_count: int
    completed_count: int
    repair_success_count: int
    repair_failure_count: int
    infrastructure_error_count: int
    unexecuted_count: int
    control_input_tokens: int | None = None
    treatment_input_tokens: int | None = None
    input_token_delta_treatment_minus_control: int | None = None
    control_duration_seconds: float | None = None
    treatment_duration_seconds: float | None = None
    duration_delta_treatment_minus_control: float | None = None
    paired_both_success_count: int = 0


class P2ExperimentSummary(BaseModel):
    """覆盖全部计划位置的脱敏 P2 汇总。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = "p2_experiment_summary"
    protocol_sha256: str
    mode: str
    planned_count: int
    completed_count: int
    repair_success_count: int
    repair_failure_count: int
    infrastructure_error_count: int
    unexecuted_count: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    task_summaries: tuple[P2TaskSummary, ...]


class P2BudgetedLLM(BaseLLM):
    """在实际供应商调用边界进行费用预留和核算。"""

    def __init__(
        self,
        config: LLMConfig,
        *,
        ledger_path: Path,
        formal: P2FormalRunRequirements,
        input_upper_bound: int,
        request_id_prefix: str = "p2",
    ) -> None:
        super().__init__(config)
        if not all(
            math.isfinite(value) and value > 0
            for value in (
                formal.total_cost_cap_usd,
                formal.input_cost_per_million_usd,
                formal.output_cost_per_million_usd,
            )
        ):
            raise BenchmarkError("P2 formal pricing must be finite and positive")
        self._delegate = LiteLLMAdapter(config.model_copy(update={"max_retries": 0}))
        self._ledger_path = ledger_path
        self._formal = formal
        self._input_upper_bound = input_upper_bound
        self._request_id_prefix = request_id_prefix
        self._request_number = 0

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec] = ()) -> LLMResponse:
        ledger = _read_cost_ledger(
            self._ledger_path,
            self._formal.total_cost_cap_usd,
            self._formal.provider,
            self._formal.model_name,
        )
        if ledger.uncertain_request or ledger.halt_reason:
            raise BenchmarkError("P2 cost ledger contains an uncertain request")
        self._request_number += 1
        request_id = f"{self._request_id_prefix}:{self._request_number}"
        reservation = estimated_request_reservation(
            self._formal,
            input_tokens=self._input_upper_bound,
            output_tokens=self.config.max_output_tokens or 0,
        )
        if ledger.spent_usd + ledger.reserved_usd + reservation > ledger.cap_usd:
            _write_cost_ledger(
                self._ledger_path,
                ledger.model_copy(update={"halt_reason": "cost_cap_would_be_exceeded"}),
            )
            raise BenchmarkError("P2 total cost cap would be exceeded")
        request = P2CostRequestRecord(
            request_id=request_id,
            status="reserved",
            reserved_usd=reservation,
            input_token_upper_bound=self._input_upper_bound,
            output_token_upper_bound=self.config.max_output_tokens or 0,
        )
        ledger = ledger.model_copy(
            update={
                "reserved_usd": ledger.reserved_usd + reservation,
                "request_count": ledger.request_count + 1,
                "uncertain_request": True,
                "requests": (*ledger.requests, request),
            }
        )
        _write_cost_ledger(self._ledger_path, ledger)
        response = self._delegate.complete(messages, tools)
        usage = response.usage
        if (
            usage.input_tokens <= 0
            or usage.output_tokens < 0
            or usage.total_tokens != usage.input_tokens + usage.output_tokens
            or usage.input_tokens > self._input_upper_bound
            or usage.output_tokens > (self.config.max_output_tokens or 0)
        ):
            raise BenchmarkError("P2 provider usage is missing, inconsistent, or exceeds its bound")
        actual = estimated_request_reservation(
            self._formal,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
        if actual > reservation or ledger.spent_usd + actual > ledger.cap_usd:
            raise BenchmarkError("P2 provider cost exceeds the persisted reservation")
        settled = request.model_copy(
            update={
                "status": "settled",
                "actual_input_tokens": usage.input_tokens,
                "actual_output_tokens": usage.output_tokens,
                "actual_cost_usd": actual,
                "actual_cost_source": "conservative_calculation_not_provider_bill",
            }
        )
        ledger = ledger.model_copy(
            update={
                "spent_usd": ledger.spent_usd + actual,
                "reserved_usd": max(0, ledger.reserved_usd - reservation),
                "uncertain_request": False,
                "requests": (*ledger.requests[:-1], settled),
            }
        )
        _write_cost_ledger(self._ledger_path, ledger)
        return response.model_copy(update={"usage": usage.model_copy(update={"cost_usd": actual})})


def estimated_request_reservation(
    formal: P2FormalRunRequirements, *, input_tokens: int, output_tokens: int
) -> float:
    """在供应商调用前按每次上限保留费用，避免超过总帽。"""
    return (
        input_tokens * formal.conservative_input_price
        + output_tokens * formal.effective_output_price
    ) / 1_000_000


def _write_cost_ledger(path: Path, ledger: P2CostLedgerRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(ledger.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _read_cost_ledger(
    path: Path,
    cap_usd: float,
    provider: str | None = None,
    model_name: str | None = None,
) -> P2CostLedgerRecord:
    if not path.is_file():
        return P2CostLedgerRecord(cap_usd=cap_usd, provider=provider, model_name=model_name)
    ledger = P2CostLedgerRecord.model_validate_json(path.read_text(encoding="utf-8"))
    if ledger.cap_usd != cap_usd:
        raise BenchmarkError("P2 cost cap does not match the persisted ledger")
    if provider is not None and (ledger.provider != provider or ledger.model_name != model_name):
        raise BenchmarkError("P2 cost ledger provider identity does not match the formal protocol")
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


def _read_trial(path: Path) -> P2TrialRecord | None:
    return (
        P2TrialRecord.model_validate_json(path.read_text(encoding="utf-8"))
        if path.is_file()
        else None
    )


def summarize_p2_experiment(experiment_dir: Path) -> P2ExperimentSummary:
    """只读取冻结协议和试次记录，覆盖每个计划位置而不补写运行结果。"""
    root = experiment_dir.expanduser().resolve()
    protocol_path = root / "protocol.json"
    protocol = P2ProtocolRecord.model_validate_json(protocol_path.read_text(encoding="utf-8"))
    records = {
        plan.sequence: _read_trial(root / "trials" / f"{plan.sequence:03d}.json")
        for plan in protocol.schedule
    }
    completed = [
        record for record in records.values() if record and record.status == "verification_complete"
    ]
    infrastructure = [
        record for record in records.values() if record and record.status == "infrastructure_error"
    ]
    successes = [record for record in completed if record and record.independent_passed]
    failures = [record for record in completed if record and not record.independent_passed]
    task_summaries: list[P2TaskSummary] = []
    for task_id in protocol.qualified_task_ids:
        task_plans = [plan for plan in protocol.schedule if plan.task_id == task_id]
        task_records = [(plan, records[plan.sequence]) for plan in task_plans]
        by_arm = {
            arm: [record for plan, record in task_records if plan.arm is arm and record]
            for arm in (ExperimentArm.CONTROL, ExperimentArm.TREATMENT)
        }

        def _known_total(items: list[P2TrialRecord], field: str) -> int | None:
            return sum(getattr(item, field) for item in items) if len(items) == 3 else None

        def _known_duration(items: list[P2TrialRecord]) -> float | None:
            values = [item.agent_duration_seconds for item in items]
            return (
                sum(values)
                if len(items) == 3 and all(value is not None for value in values)
                else None
            )

        control_tokens = _known_total(by_arm[ExperimentArm.CONTROL], "input_tokens")
        treatment_tokens = _known_total(by_arm[ExperimentArm.TREATMENT], "input_tokens")
        control_duration = _known_duration(by_arm[ExperimentArm.CONTROL])
        treatment_duration = _known_duration(by_arm[ExperimentArm.TREATMENT])
        paired = 0
        for repetition in range(1, 4):
            pair = [
                record for plan, record in task_records if plan.repetition == repetition and record
            ]
            if len(pair) == 2 and all(record.independent_passed for record in pair):
                paired += 1
        task_summaries.append(
            P2TaskSummary(
                task_id=task_id,
                qualification_type=protocol.p1_qualifications[task_id].qualification_type,
                planned_count=6,
                completed_count=sum(
                    record.status == "verification_complete" for _, record in task_records if record
                ),
                repair_success_count=sum(
                    bool(record and record.independent_passed) for _, record in task_records
                ),
                repair_failure_count=sum(
                    bool(
                        record
                        and record.status == "verification_complete"
                        and not record.independent_passed
                    )
                    for _, record in task_records
                ),
                infrastructure_error_count=sum(
                    bool(record and record.status == "infrastructure_error")
                    for _, record in task_records
                ),
                unexecuted_count=sum(
                    record is None
                    or record.status not in {"verification_complete", "infrastructure_error"}
                    for _, record in task_records
                ),
                control_input_tokens=control_tokens,
                treatment_input_tokens=treatment_tokens,
                input_token_delta_treatment_minus_control=(
                    treatment_tokens - control_tokens
                    if control_tokens is not None and treatment_tokens is not None
                    else None
                ),
                control_duration_seconds=control_duration,
                treatment_duration_seconds=treatment_duration,
                duration_delta_treatment_minus_control=(
                    treatment_duration - control_duration
                    if control_duration is not None and treatment_duration is not None
                    else None
                ),
                paired_both_success_count=paired,
            )
        )
    all_complete = len(completed) == len(protocol.schedule)
    return P2ExperimentSummary(
        protocol_sha256=_sha256(protocol_path),
        mode=(completed[0].mode if completed else "unknown"),
        planned_count=len(protocol.schedule),
        completed_count=len(completed),
        repair_success_count=len(successes),
        repair_failure_count=len(failures),
        infrastructure_error_count=len(infrastructure),
        unexecuted_count=len(protocol.schedule) - len(completed) - len(infrastructure),
        input_tokens=sum(record.input_tokens for record in completed) if all_complete else None,
        output_tokens=sum(record.output_tokens for record in completed) if all_complete else None,
        cost_usd=(
            sum(record.cost_usd or 0 for record in completed)
            if all_complete and all(record.cost_usd is not None for record in completed)
            else None
        ),
        task_summaries=tuple(task_summaries),
    )


def write_p2_summary(experiment_dir: Path) -> Path:
    """输出机器可读汇总和紧邻的中文说明，不执行 Agent 或供应商请求。"""
    root = experiment_dir.expanduser().resolve()
    summary = summarize_p2_experiment(root)
    path = root / "p2-summary.json"
    path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    report = root / "p2-summary.md"
    report.write_text(
        "# P2 实验汇总\n\n"
        f"计划 {summary.planned_count} 次，完成 {summary.completed_count} 次；"
        f"独立验收成功 {summary.repair_success_count} 次，失败 {summary.repair_failure_count} 次，"
        f"基础设施错误 {summary.infrastructure_error_count} 次，"
        f"未执行 {summary.unexecuted_count} 次。\n\n"
        "模拟结果仅验证工程流程，不构成 Agent 修复能力结论。\n",
        encoding="utf-8",
    )
    return path


def _freeze_p1_qualifications(
    path: Path | None, tasks: tuple[RealIssueTask, ...]
) -> tuple[str | None, dict[str, P2QualificationRecord]]:
    """读取 P1 原始行为记录，而不是复述其摘要数字。"""
    if path is None:
        return None, {}
    try:
        raw = path.expanduser().resolve().read_bytes()
        records = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError("P2 cannot read the required P1 qualification evidence") from exc
    if not isinstance(records, list):
        raise BenchmarkError("P1 qualification evidence must be a JSON list")
    by_task: dict[str, dict[str, object]] = {}
    for item in records:
        if not isinstance(item, dict) or not isinstance(item.get("task_id"), str):
            raise BenchmarkError("P1 qualification evidence has an invalid task record")
        task_id = item["task_id"]
        if task_id in by_task:
            raise BenchmarkError(
                "P1 qualification evidence has duplicate task records", context={"task_id": task_id}
            )
        by_task[task_id] = item
    frozen: dict[str, P2QualificationRecord] = {}
    for task in tasks:
        item = by_task.get(task.id)
        if item is None:
            raise BenchmarkError(
                "P1 qualification evidence is missing a P2 task", context={"task_id": task.id}
            )
        qualification = item.get("qualification_type")
        expected_type = (
            "expected_collection_failure"
            if task.id in COLLECTION_FAILURE_TASK_IDS
            else "assertion_failure"
        )
        if qualification != expected_type or item.get("base_commit") != task.base_commit:
            raise BenchmarkError(
                "P1 qualification identity does not match the fixed task",
                context={"task_id": task.id},
            )
        environment = item.get("test_environment")
        gold = item.get("gold_evidence")
        if not isinstance(environment, dict) or not isinstance(gold, dict):
            raise BenchmarkError(
                "P1 qualification evidence is structurally incomplete", context={"task_id": task.id}
            )
        fingerprint = environment.get("fingerprint_sha256")
        nodes = gold.get("executed_node_ids")
        complete_gold = (
            gold.get("status") == "passed"
            and gold.get("returncode") == 0
            and gold.get("audit_available") is True
            and gold.get("collection_audit_available") is True
            and gold.get("source_import_audit_valid") is True
            and gold.get("skipped_count") == 0
            and gold.get("xfailed_count") == 0
            and gold.get("xpassed_count") == 0
        )
        if (
            not isinstance(fingerprint, str)
            or not isinstance(nodes, list)
            or not nodes
            or not complete_gold
        ):
            raise BenchmarkError(
                "P1 qualification lacks complete gold execution evidence",
                context={"task_id": task.id},
            )
        if not all(isinstance(node, str) for node in nodes) or len(set(nodes)) != len(nodes):
            raise BenchmarkError(
                "P1 qualification has invalid frozen node IDs", context={"task_id": task.id}
            )
        _validate_p1_gold_artifacts(task.id, gold, tuple(nodes))
        frozen[task.id] = P2QualificationRecord(
            task_id=task.id,
            qualification_type=qualification,
            base_commit=task.base_commit,
            dependency_fingerprint=fingerprint,
            expected_node_ids=tuple(nodes),
        )
    return hashlib.sha256(raw).hexdigest(), frozen


def _validate_p1_gold_artifacts(task_id: str, gold: object, nodes: tuple[str, ...]) -> None:
    """核对保留的 P1 审计与 JUnit，而不是只相信汇总中的布尔值。"""
    assert isinstance(gold, dict)
    audit_path = gold.get("audit_path")
    if not isinstance(audit_path, str):
        raise BenchmarkError(
            "P1 gold evidence is missing its execution audit", context={"task_id": task_id}
        )
    audit_file = Path(audit_path)
    collection_file = audit_file.with_name("collection.audit.json")
    junit_file = audit_file.parent / "junit.xml"
    try:
        execution = json.loads(audit_file.read_text(encoding="utf-8"))
        collection = json.loads(collection_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(
            "P1 gold audit artifacts are unavailable", context={"task_id": task_id}
        ) from exc
    if not junit_file.is_file():
        raise BenchmarkError("P1 gold JUnit artifact is unavailable", context={"task_id": task_id})
    executed = execution.get("collected_node_ids")
    reports = execution.get("reports")
    expected_stages = {(node, stage) for node in nodes for stage in ("setup", "call", "teardown")}
    actual_stages = (
        {
            (report.get("nodeid"), report.get("when"))
            for report in reports
            if isinstance(report, dict) and report.get("outcome") == "passed"
        }
        if isinstance(reports, list)
        else set()
    )
    if (
        execution.get("stage") != "execution"
        or execution.get("completed") is not True
        or execution.get("exitstatus") != 0
        or not isinstance(executed, list)
        or tuple(executed) != nodes
        or actual_stages != expected_stages
        or collection.get("stage") != "collection"
        or collection.get("completed") is not True
        or collection.get("exitstatus") != 0
        or tuple(collection.get("collected_node_ids", ())) != nodes
    ):
        raise BenchmarkError(
            "P1 gold audit artifacts conflict with its qualification", context={"task_id": task_id}
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
        (
            (ExperimentArm.CONTROL, ExperimentArm.TREATMENT),
            (ExperimentArm.TREATMENT, ExperimentArm.CONTROL),
            (ExperimentArm.CONTROL, ExperimentArm.TREATMENT),
        ),
        start=1,
    ):
        for task_id in task_ids:
            for arm in arms:
                enabled = arm is ExperimentArm.TREATMENT
                plans.append(
                    P2TrialPlan(
                        sequence=len(plans) + 1,
                        task_id=task_id,
                        repetition=repetition,
                        arm=arm,
                        token_optimization_enabled=enabled,
                        repo_map_enabled=enabled,
                        context_compaction_enabled=enabled,
                    )
                )
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
    p1_sha256, p1_qualifications = _freeze_p1_qualifications(config.p1_evidence_path, tasks)
    missing = (
        ()
        if config.formal
        else (
            "model_name",
            "provider",
            "pricing_source",
            "total_cost_cap_usd",
            "input_cost_per_million_usd",
            "output_cost_per_million_usd",
        )
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
        p1_evidence_sha256=p1_sha256,
        p1_qualifications=p1_qualifications,
        code_hashes=_code_hashes(repository_root),
        budgets={
            "max_input_tokens": config.max_input_tokens,
            "max_output_tokens": config.max_output_tokens,
            "max_steps": config.max_steps,
            "max_test_runs": config.max_test_runs,
            "wall_time_seconds": config.wall_time_seconds,
            "per_request_input_tokens": config.per_request_input_tokens,
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
    if config.p1_evidence_path is None:
        raise BenchmarkError("P2 input check requires the retained P1 qualification evidence")
    protocol = build_p2_protocol(config, repository_root=repository_root)
    tasks = load_real_issue_tasks(config.tasks_dir, task_ids=protocol.qualified_task_ids)
    recipes = load_environment_recipes(config.recipes_dir)
    inputs: list[P2InputCheck] = []
    for task in tasks:
        source = (config.source_root / task.id).expanduser().resolve()
        task.validate_checkout(source)
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=source,
            capture_output=True,
            text=True,
            check=False,
        )
        if dirty.returncode or dirty.stdout.strip():
            raise BenchmarkError("P2 source checkout is not clean", context={"task_id": task.id})
        python = resolve_managed_environment_python(config.test_env_root, task.id)
        provenance = inspect_test_environment(python, pythonpath_entries=task.test_pythonpath_paths)
        frozen = protocol.p1_qualifications.get(task.id)
        if frozen is None or provenance.fingerprint_sha256 != frozen.dependency_fingerprint:
            raise BenchmarkError(
                "P2 environment differs from the P1-qualified environment",
                context={"task_id": task.id},
            )
        inputs.append(
            P2InputCheck(
                task_id=task.id,
                source=str(source),
                source_commit=task.base_commit,
                source_clean=True,
                recipe_fingerprint=recipes[task.id].fingerprint,
                test_python=str(python),
                dependency_fingerprint=provenance.fingerprint_sha256,
            )
        )
    tracked_diff = _tracked_diff(repository_root)
    if config.formal is not None and tracked_diff:
        raise BenchmarkError("formal P2 run requires a clean tracked worktree")
    return P2CheckRecord(
        generated_at=datetime.now(UTC),
        code_commit=_git_commit(repository_root),
        tracked_worktree_dirty=bool(tracked_diff),
        tracked_diff_sha256=hashlib.sha256(tracked_diff).hexdigest(),
        protocol=protocol,
        inputs=tuple(inputs),
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
    config: P2ProtocolConfig, *, experiment_dir: Path, mode: str, repository_root: Path = Path(".")
) -> P2RunSummary:
    """执行并恢复统一 P2 流程；仅 formal 模式创建供应商客户端。"""
    if mode not in {"simulation", "formal"}:
        raise BenchmarkError("unknown P2 execution mode", context={"mode": mode})
    if mode == "formal" and config.formal is None:
        raise BenchmarkError("formal P2 run requires complete commercial parameters")
    check = check_p2_inputs(config, repository_root=repository_root)
    protocol = check.protocol
    root = experiment_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
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
    trials_root = root / "trials"
    trials_root.mkdir(parents=True, exist_ok=True)
    protocol_path = root / "protocol.json"
    if protocol_path.exists():
        persisted = P2ProtocolRecord.model_validate_json(protocol_path.read_text(encoding="utf-8"))
        comparable = protocol.model_copy(update={"generated_at": persisted.generated_at})
        if comparable != persisted:
            raise BenchmarkError("P2 resume protocol identity does not match")
    else:
        protocol_path.write_text(protocol.model_dump_json(indent=2), encoding="utf-8")
    results: list[P2TrialRecord] = []
    tasks = {
        item.id: item
        for item in load_real_issue_tasks(config.tasks_dir, task_ids=protocol.qualified_task_ids)
    }
    recipes = load_environment_recipes(config.recipes_dir)
    if mode == "simulation":
        runner = TraceFixRunner(P2SimulationLLM)
        model_name = "tracefix/p2-simulation"
    else:
        assert config.formal is not None
        ledger_path = root / "cost-ledger.json"
        formal = config.formal
        request_prefix = ["unassigned"]

        def formal_factory(llm_config: LLMConfig) -> BaseLLM:
            return P2BudgetedLLM(
                llm_config,
                ledger_path=ledger_path,
                formal=formal,
                input_upper_bound=config.per_request_input_tokens,
                request_id_prefix=request_prefix[0],
            )

        runner = TraceFixRunner(formal_factory)
        model_name = formal.model_name
    try:
        for plan in protocol.schedule:
            path = trials_root / f"{plan.sequence:03d}.json"
            existing = _read_trial(path)
            if existing and existing.status == "verification_complete":
                _verify_saved_artifacts(existing)
                results.append(existing.model_copy(update={"resumed": True}))
                continue
            if existing and existing.status not in {"agent_completed"}:
                raise BenchmarkError(
                    "P2 trial has uncertain prior state; do not resend request",
                    context={"path": str(path), "status": existing.status},
                )
            task = tasks[plan.task_id]
            if existing:
                _verify_saved_artifacts(existing)
                agent_record = existing.model_copy(update={"resumed": True})
            else:
                attempt_id = f"p2-{plan.sequence:03d}-{uuid4().hex}"
                write_trial_record(
                    path,
                    P2TrialRecord(
                        sequence=plan.sequence,
                        task_id=plan.task_id,
                        arm=plan.arm,
                        repetition=plan.repetition,
                        mode=mode,
                        status="agent_running",
                        attempt_id=attempt_id,
                    ),
                )
                enabled = plan.arm is ExperimentArm.TREATMENT
                agent_config = AgentConfig(
                    max_steps=config.max_steps,
                    max_input_tokens=config.max_input_tokens,
                    max_output_tokens=config.max_output_tokens,
                    wall_time_seconds=config.wall_time_seconds,
                    max_test_runs=config.max_test_runs,
                    token_optimization_enabled=enabled,
                    context=ContextConfig(
                        enabled=enabled, compaction_trigger_tokens=config.context_trigger_tokens
                    ),
                    repo_map=RepoMapConfig(enabled=enabled),
                    record_request_views=True,
                )
                if mode == "formal":
                    request_prefix[0] = attempt_id
                result = runner.run(
                    RunConfig(
                        repo=config.source_root / task.id,
                        task=task.problem_statement,
                        model_name=model_name,
                        output_dir=root / "agent-runs",
                        # 正式实验只从本机 .env 加载凭据；模拟模式保持完全离线，
                        # 避免测试或工程演练接触供应商配置。
                        env_file=Path(".env") if mode == "formal" else None,
                        llm_max_retries=0,
                        per_request_output_tokens=config.per_request_output_tokens,
                        test_python_executable=resolve_managed_environment_python(
                            config.test_env_root, task.id
                        ),
                        test_pythonpath_entries=task.test_pythonpath_paths,
                        agent_config=agent_config,
                    )
                )
                agent_record = P2TrialRecord(
                    sequence=plan.sequence,
                    task_id=plan.task_id,
                    arm=plan.arm,
                    repetition=plan.repetition,
                    mode=mode,
                    status="agent_completed",
                    attempt_id=attempt_id,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cost_usd=result.cost_usd,
                    stop_reason=result.stop_reason,
                    run_result_path=result.result_path,
                    agent_patch_path=result.diff_path,
                    agent_duration_seconds=result.duration_seconds,
                    run_result_sha256=_artifact_sha256(result.result_path),
                    agent_patch_sha256=_artifact_sha256(result.diff_path),
                )
                write_trial_record(path, agent_record)
                if mode == "formal" and config.formal is not None:
                    ledger = _read_cost_ledger(
                        root / "cost-ledger.json",
                        config.formal.total_cost_cap_usd,
                        config.formal.provider,
                        config.formal.model_name,
                    )
                    if ledger.uncertain_request or ledger.halt_reason:
                        stopped = agent_record.model_copy(update={"status": "request_uncertain"})
                        write_trial_record(path, stopped)
                        raise BenchmarkError(
                            "P2 formal run stopped before another provider request"
                        )
            if not agent_record.agent_patch_path:
                record = agent_record.model_copy(update={"status": "infrastructure_error"})
                write_trial_record(path, record)
                results.append(record)
                continue
            verification_started = time.monotonic()
            verification = validate_agent_patch_strict(
                task,
                source=config.source_root / task.id,
                agent_patch=Path(agent_record.agent_patch_path),
                test_python=resolve_managed_environment_python(config.test_env_root, task.id),
                output_dir=root / "verification" / f"{plan.sequence:03d}",
                recipe=recipes[task.id],
                expected_node_ids=protocol.p1_qualifications[task.id].expected_node_ids,
            )
            verification_path = root / "verification" / f"{plan.sequence:03d}" / "result.json"
            verification_path.parent.mkdir(parents=True, exist_ok=True)
            verification_path.write_text(verification.model_dump_json(indent=2), encoding="utf-8")
            record = agent_record.model_copy(
                update={
                    "status": "verification_complete",
                    "independent_passed": verification.eligible,
                    "verification_eligible": verification.eligible,
                    "verification_path": str(verification_path),
                    "verification_duration_seconds": time.monotonic() - verification_started,
                    "verification_sha256": _artifact_sha256(str(verification_path)),
                }
            )
            write_trial_record(path, record)
            results.append(record)
    finally:
        if lock_path.exists():
            lock_path.unlink()
    summary_path = root / "summary.json"
    summary = P2RunSummary(
        mode=mode,
        protocol_path=str(protocol_path),
        trial_count=len(results),
        completed_count=sum(item.status == "verification_complete" for item in results),
        resumed_count=sum(item.resumed for item in results),
        input_tokens=sum(item.input_tokens for item in results),
        output_tokens=sum(item.output_tokens for item in results),
        cost_usd=sum(item.cost_usd or 0 for item in results),
        results=tuple(results),
        engineering_simulation_only=mode == "simulation",
        summary_path=str(summary_path),
    )
    temporary = summary_path.with_suffix(".tmp")
    temporary.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary, summary_path)
    return summary


def run_p2_simulation(
    config: P2ProtocolConfig, *, experiment_dir: Path, repository_root: Path = Path(".")
) -> P2RunSummary:
    return run_p2_experiment(
        config,
        experiment_dir=experiment_dir,
        mode="simulation",
        repository_root=repository_root,
    )


def run_p2_formal(
    config: P2ProtocolConfig, *, experiment_dir: Path, repository_root: Path = Path(".")
) -> P2RunSummary:
    return run_p2_experiment(
        config,
        experiment_dir=experiment_dir,
        mode="formal",
        repository_root=repository_root,
    )
