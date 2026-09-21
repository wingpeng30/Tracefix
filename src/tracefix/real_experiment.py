"""真实 GitHub Issue 的行为校验、32k 预筛选与条件配对实验。"""
# ruff: noqa: E501

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as element_tree
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
from tracefix.real_environment import resolve_managed_environment_python
from tracefix.real_recipes import EnvironmentRecipe, ExpectedBaseFailure
from tracefix.runtime import (
    DEFAULT_MODEL_NAME,
    DEFAULT_USD_CNY_RATE,
    RunConfig,
    RunResult,
    TraceFixRunner,
)
from tracefix.tools import RunTestsTool, ToolResult

P2_SCORING_CONTRACT_VERSION = "p2-scoring-v1"
P2_FORBIDDEN_CONFIG_NAMES = frozenset(
    {"conftest.py", "pytest.ini", "tox.ini", "pyproject.toml", "setup.cfg"}
)


def p2_scoring_instruction() -> str:
    """Return the exact Agent-visible rule enforced by strict P2 scoring."""
    return (
        "本次 P2 独立验收禁止修改、新增、删除或重命名测试、conftest、pytest 配置"
        "及 setup.cfg；只修改产品源码或必要的非测试文档。可运行现有测试，但不要提交测试改动。"
    )


def classify_p2_changed_paths(
    task: RealIssueTask, paths: tuple[str, ...]
) -> tuple[str | None, tuple[str, ...]]:
    """Classify changed paths with the same exact rules used by strict scoring."""
    return classify_p2_paths(paths, expected_test_files=task.expected_test_files)


def classify_p2_paths(
    paths: tuple[str, ...], *, expected_test_files: tuple[str, ...] = ()
) -> tuple[str | None, tuple[str, ...]]:
    """Classify P2 paths without substring matching or checkout access."""
    normalized = tuple(Path(path).as_posix() for path in paths)
    expected = {Path(path).as_posix() for path in expected_test_files}
    forbidden = {
        path
        for path in normalized
        if path in expected
        or path.startswith(("tests/", "testing/"))
        or Path(path).name.startswith("test_")
    }
    forbidden.update(path for path in normalized if Path(path).name in P2_FORBIDDEN_CONFIG_NAMES)
    if not forbidden:
        return None, ()
    config_changed = any(Path(path).name in P2_FORBIDDEN_CONFIG_NAMES for path in forbidden)
    test_paths = {path for path in forbidden if Path(path).name not in P2_FORBIDDEN_CONFIG_NAMES}
    product_python = {
        path for path in normalized if path.endswith(".py") and path not in forbidden
    }
    category = (
        "config_modified"
        if config_changed
        else "source_and_test"
        if product_python and test_paths
        else "test_only"
    )
    return category, tuple(sorted(forbidden))


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
    environment_before: TestEnvironmentProvenance
    environment_after_initial: TestEnvironmentProvenance
    environment_after_gold: TestEnvironmentProvenance
    dependency_drift_detected: bool = False
    initial_evidence: PytestExecutionEvidence
    gold_evidence: PytestExecutionEvidence
    eligible_for_llm_prescreen: bool
    qualification_type: str
    eligibility_reason: str | None = None


class AgentPatchValidation(BaseModel):
    """P2 对 Agent 补丁的单次严格独立验收结果。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    patch_applied: bool
    eligible: bool
    reason: str | None = None
    evidence: PytestExecutionEvidence | None = None
    environment_before: TestEnvironmentProvenance
    environment_after: TestEnvironmentProvenance
    dependency_drift_detected: bool = False


class ProcessEvidence(BaseModel):
    """一次构建、收集或测试子进程的完整可追溯证据。"""

    model_config = ConfigDict(extra="forbid")

    stage: str
    command: tuple[str, ...]
    working_directory: str
    returncode: int | None
    timed_out: bool
    duration_ms: float = Field(ge=0)
    stdout_path: str
    stderr_path: str


class CollectionErrorEvidence(BaseModel):
    """收集阶段的结构化异常；入口与正文都参与受审查例外判断。"""

    model_config = ConfigDict(extra="forbid")

    nodeid: str
    longrepr: str


class PytestExecutionEvidence(BaseModel):
    """一次 pytest 验收的收集、执行、JUnit 与插件结构化证据。"""

    model_config = ConfigDict(extra="forbid")

    status: str
    returncode: int | None
    timed_out: bool
    junit_available: bool
    audit_available: bool = False
    collection_audit_available: bool = False
    execution_audit_available: bool = False
    audit_diagnostic: str | None = None
    source_import_audit_valid: bool = False
    imported_source_paths: dict[str, str] = Field(default_factory=dict)
    collection_errors: tuple[str, ...] = ()
    collection_error_records: tuple[CollectionErrorEvidence, ...] = ()
    test_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    xfailed_count: int = Field(ge=0)
    xpassed_count: int = Field(ge=0)
    collected_node_ids: tuple[str, ...] = ()
    executed_node_ids: tuple[str, ...] = ()
    expected_node_ids: tuple[str, ...] = ()
    import_probe_path: str | None = None
    diagnostic: str | None = None
    output_tail: str = ""
    build_steps: tuple[ProcessEvidence, ...] = ()
    collection: ProcessEvidence | None = None
    execution: ProcessEvidence | None = None
    audit_path: str | None = None
    collection_exception_type: str | None = None
    collection_exception_module: str | None = None
    collection_exception_symbol: str | None = None


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
    recipe: EnvironmentRecipe | None = None,
) -> RealTaskBehaviorValidation:
    """在独立副本中用 JUnit 证明目标测试先失败、gold 后通过。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    task.validate_checkout(source)
    environment_before = inspect_test_environment(
        test_python, pythonpath_entries=task.test_pythonpath_paths
    )
    results: dict[str, ToolResult] = {}
    for variant in ("initial", "gold"):
        checkout = output_dir / f"{task.id}-{variant}"
        if checkout.exists():
            raise BenchmarkError(
                "behavior validation destination already exists",
                context={"task_id": task.id, "variant": variant},
            )
        _create_behavior_checkout(source, checkout)
        _git(["apply", str(task.test_patch_path)], checkout)
        if variant == "gold":
            _git(["apply", str(task.gold_patch_path)], checkout)
        results[variant] = _run_qualified_pytest(
            task, checkout, test_python, f"behavior-{task.id}-{variant}", recipe
        )
        if variant == "initial":
            environment_after_initial = inspect_test_environment(
                test_python, pythonpath_entries=task.test_pythonpath_paths
            )
    environment_after_gold = inspect_test_environment(
        test_python, pythonpath_entries=task.test_pythonpath_paths
    )
    dependency_drift_detected = len(
        {
            environment_before.fingerprint_sha256,
            environment_after_initial.fingerprint_sha256,
            environment_after_gold.fingerprint_sha256,
        }
    ) != 1
    initial_evidence = _pytest_evidence(results["initial"], output_dir / f"{task.id}-initial")
    gold_evidence = _pytest_evidence(results["gold"], output_dir / f"{task.id}-gold")
    # 绝不以测试数量替代实际 node ID。参数化名称和目录都属于目标集合的一部分；
    # 缺失结构化执行记录时 fail closed，避免把不完整报告误记为有效复现。
    initial_nodes = _canonical_node_ids(
        initial_evidence.executed_node_ids, output_dir / f"{task.id}-initial"
    )
    gold_nodes = _canonical_node_ids(
        gold_evidence.executed_node_ids, output_dir / f"{task.id}-gold"
    )
    same_execution_set = bool(initial_nodes) and initial_nodes == gold_nodes
    no_nonbusiness_outcomes = all(
        item.skipped_count == 0 and item.xfailed_count == 0 and item.xpassed_count == 0
        for item in (initial_evidence, gold_evidence)
    )
    expected_collection = _matches_expected_collection_failure(
        initial_evidence, recipe.expected_base_failure if recipe else None
    )
    initial_failed = (
        initial_evidence.status == "assertion_failed"
        and initial_evidence.collection_audit_available
        and same_execution_set
    )
    initial_collection_failed = expected_collection and bool(gold_nodes) and gold_evidence.audit_available
    gold_passed = (
        gold_evidence.status == "passed"
        and gold_evidence.junit_available
        and gold_evidence.audit_available
        and gold_evidence.collection_audit_available
        and bool(gold_nodes)
        and no_nonbusiness_outcomes
    )
    eligible = (initial_failed or initial_collection_failed) and gold_passed and not dependency_drift_detected
    reasons = []
    if not initial_failed and not initial_collection_failed:
        reasons.append(f"initial={initial_evidence.status}")
    if not gold_passed:
        reasons.append(f"gold={gold_evidence.status}")
    if not same_execution_set and not initial_collection_failed:
        reasons.append("base/gold executed node IDs differ or are missing")
    if not no_nonbusiness_outcomes:
        reasons.append("skip/xfail/xpass was observed")
    if dependency_drift_detected:
        reasons.append("test environment dependency fingerprint drifted during validation")
    reason = "; ".join(reasons) or None
    if eligible and initial_collection_failed:
        qualification_type = "expected_collection_failure"
    elif eligible:
        qualification_type = "assertion_failure"
    elif initial_evidence.status == "passed":
        qualification_type = "business_not_reproduced"
    elif initial_evidence.status in {
        "permission_error",
        "network_error",
        "dependency_error",
        "collection_error",
        "build_error",
        "source_import_error",
        "timeout",
    }:
        qualification_type = "environment_blocked"
    else:
        qualification_type = "behavior_unqualified"
    return RealTaskBehaviorValidation(
        task_id=task.id,
        base_commit=task.base_commit,
        initial_hidden_failed=initial_failed,
        gold_hidden_passed=gold_passed,
        initial_returncode=_returncode(results["initial"]),
        gold_returncode=_returncode(results["gold"]),
        test_command=task.test_command,
        test_environment=inspect_test_environment(
            test_python, pythonpath_entries=task.test_pythonpath_paths
        ),
        environment_before=environment_before,
        environment_after_initial=environment_after_initial,
        environment_after_gold=environment_after_gold,
        dependency_drift_detected=dependency_drift_detected,
        initial_evidence=initial_evidence,
        gold_evidence=gold_evidence,
        eligible_for_llm_prescreen=eligible,
        qualification_type=qualification_type,
        eligibility_reason=reason,
    )


def validate_agent_patch_strict(
    task: RealIssueTask, *, source: Path, agent_patch: Path, test_python: Path,
    output_dir: Path, recipe: EnvironmentRecipe | None = None,
    expected_node_ids: tuple[str, ...] | None = None,
) -> AgentPatchValidation:
    """在全新 checkout 中验收 Agent 补丁，拒绝不完整证据与环境漂移。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    task.validate_checkout(source)
    before = inspect_test_environment(test_python, pythonpath_entries=task.test_pythonpath_paths)
    checkout = output_dir / f"{task.id}-agent"
    _create_behavior_checkout(source, checkout)
    if not agent_patch.is_file() or not agent_patch.stat().st_size:
        return AgentPatchValidation(
            task_id=task.id, patch_applied=False, eligible=False, reason="empty_agent_patch",
            environment_before=before, environment_after=before,
        )
    try:
        _git(["apply", str(agent_patch)], checkout)
        # ``git diff`` 不会列出 Agent 新建但尚未追踪的文件；测试篡改也可能
        # 恰好以这种形式出现。因此合并 HEAD 差异和未追踪文件清单。
        # NUL 分隔避免 Windows 路径、空格或重命名记录被按行截断；name-status
        # 的 rename/copy 记录携带旧、新两个路径，二者都必须通过保护检查。
        status_fields = _git(["diff", "--name-status", "-z", "HEAD"], checkout).stdout.split("\0")
        changed: list[str] = []
        index = 0
        while index < len(status_fields) and status_fields[index]:
            status = status_fields[index]
            index += 1
            path_count = 2 if status[:1] in {"R", "C"} else 1
            changed.extend(field for field in status_fields[index:index + path_count] if field)
            index += path_count
        changed.extend(
            field for field in _git(
                ["ls-files", "-z", "--others", "--exclude-standard"], checkout
            ).stdout.split("\0") if field
        )
        _, forbidden = classify_p2_changed_paths(task, tuple(changed))
        if forbidden:
            after = inspect_test_environment(
                test_python, pythonpath_entries=task.test_pythonpath_paths
            )
            return AgentPatchValidation(
                task_id=task.id, patch_applied=True, eligible=False,
                reason="agent_modified_test_or_pytest_configuration",
                environment_before=before, environment_after=after,
                dependency_drift_detected=before.fingerprint_sha256 != after.fingerprint_sha256,
            )
        _git(["apply", str(task.test_patch_path)], checkout)
    except BenchmarkError as exc:
        after = inspect_test_environment(test_python, pythonpath_entries=task.test_pythonpath_paths)
        return AgentPatchValidation(
            task_id=task.id, patch_applied=False, eligible=False, reason=str(exc),
            environment_before=before, environment_after=after,
            dependency_drift_detected=before.fingerprint_sha256 != after.fingerprint_sha256,
        )
    result = _run_qualified_pytest(task, checkout, test_python, f"p2-{task.id}-agent", recipe)
    after = inspect_test_environment(test_python, pythonpath_entries=task.test_pythonpath_paths)
    evidence = _pytest_evidence(result, checkout)
    drift = before.fingerprint_sha256 != after.fingerprint_sha256
    frozen_nodes_match = (
        expected_node_ids is None
        or _canonical_node_ids(evidence.executed_node_ids, checkout)
        == _canonical_node_ids(expected_node_ids, checkout)
    )
    eligible = (
        evidence.status == "passed" and evidence.junit_available and evidence.audit_available
        and evidence.collection_audit_available and evidence.source_import_audit_valid
        and bool(evidence.executed_node_ids) and evidence.skipped_count == 0
        and evidence.xfailed_count == 0 and evidence.xpassed_count == 0 and not drift
        and frozen_nodes_match
    )
    return AgentPatchValidation(
        task_id=task.id, patch_applied=True, eligible=eligible,
        reason=(
            None if eligible else
            evidence.audit_diagnostic or evidence.diagnostic if evidence.status == "collection_error" else
            "executed_node_ids_differ_from_frozen_p1_set" if not frozen_nodes_match else
            evidence.audit_diagnostic or evidence.diagnostic or evidence.status
        ),
        evidence=evidence, environment_before=before, environment_after=after,
        dependency_drift_detected=drift,
    )


def _canonical_node_ids(node_ids: tuple[str, ...], checkout: Path) -> frozenset[str]:
    """只移除当前 checkout 绝对前缀，完整保留目录与参数化标识。"""
    normalized: set[str] = set()
    roots = (
        str(checkout.resolve()).rstrip("\\/") + "\\",
        str(checkout.resolve()).replace("\\", "/").rstrip("/") + "/",
    )
    for node_id in node_ids:
        path_part, separator, remainder = node_id.partition("::")
        value = path_part.replace("\\", "/") + (separator + remainder if separator else "")
        for root in roots:
            normalized_root = root.replace("\\", "/")
            if value.startswith(normalized_root):
                value = value[len(normalized_root) :]
                break
        normalized.add(value)
    return frozenset(normalized)


def _matches_expected_collection_failure(
    evidence: PytestExecutionEvidence, rule: ExpectedBaseFailure | None
) -> bool:
    """仅接受配方精确声明的 collection ImportError，普通收集错误仍不合格。"""
    return bool(
        rule
        and evidence.status == "collection_error"
        and evidence.collection is not None
        and evidence.collection.returncode not in (0, None)
        and evidence.collection_audit_available
        and len(evidence.collection_error_records) == 1
        and evidence.collection_error_records[0].nodeid.replace("\\", "/") == rule.test_entry
        and _collection_exception(evidence.collection_error_records[0].longrepr)
        == (rule.exception_type, rule.module, rule.symbol)
    )


def _create_behavior_checkout(source: Path, checkout: Path) -> None:
    """创建独立工作副本；本地 clone 受限时回退到 Git worktree。

    某些 Windows Git 安装在 file-transport clone 时会调用 MSYS shell，受策略限制可能
    无法建立信号管道。worktree 仍会生成独立工作目录，补丁只修改该目录，且不会让
    base/gold 导入同一源码副本，因此满足行为验收的源码隔离要求。
    """
    try:
        _git(["clone", "--quiet", "--no-hardlinks", str(source), str(checkout)], source.parent)
    except BenchmarkError:
        _git(["worktree", "add", "--detach", str(checkout), "HEAD"], source)


def _run_qualified_pytest(
    task: RealIssueTask,
    checkout: Path,
    test_python: Path,
    call_id: str,
    recipe: EnvironmentRecipe | None = None,
) -> ToolResult:
    """用参数列表执行严格 pytest 验收，并完整保留每个阶段的日志。"""
    evidence_root = checkout / ".tracefix-validation"
    evidence_root.mkdir(parents=True, exist_ok=True)
    audit_run_id = f"{call_id}:{uuid4().hex}"
    build_steps = _run_recipe_build(recipe, checkout, test_python, evidence_root)
    failed_build = next((step for step in build_steps if step.returncode != 0 or step.timed_out), None)
    if failed_build:
        return _failed_validation_result(
            call_id,
            "recipe build command failed",
            audit_run_id=audit_run_id,
            source_import_probe=recipe.source_import_probe if recipe else None,
            build_steps=build_steps,
            execution=None,
            collection=None,
        )
    expected = (
        tuple(recipe.selector_overrides)
        if recipe and recipe.selector_overrides
        else task.fail_to_pass
    )
    command = _pytest_command(test_python, checkout, expected, evidence_root)
    environment = _validation_environment(checkout, task.test_pythonpath_paths)
    if recipe and recipe.source_import_probe:
        environment["TRACEFIX_SOURCE_IMPORT_PROBE"] = recipe.source_import_probe
    _write_audit_plugin(checkout)
    collection = _run_validation_process(
        (*command, "--collect-only", "-p", "tracefix_pytest_audit"),
        checkout,
        environment | {
            "TRACEFIX_AUDIT_PATH": str(evidence_root / "collection.audit.json"),
            "TRACEFIX_AUDIT_RUN_ID": f"{audit_run_id}:collection",
            "TRACEFIX_AUDIT_STAGE": "collection",
        },
        evidence_root,
        "collection",
    )
    collected, collection_audit = _read_audit(evidence_root / "collection.audit.json")
    if not collected:
        # 插件审计文件不可写时，pytest collect-only 的标准 node ID 仍是可靠后备证据。
        collected = _node_ids_from_collect_output(_read_text(Path(collection.stdout_path)))
    if collection.returncode != 0:
        return _failed_validation_result(
            call_id,
            "pytest collection failed",
            audit_run_id=audit_run_id,
            source_import_probe=recipe.source_import_probe if recipe else None,
            build_steps=build_steps,
            collection=collection,
            execution=None,
            collected=collected,
            expected=expected,
            audit_path=collection_audit,
        )
    if not expected or not _selectors_collected(expected, collected):
        return _failed_validation_result(
            call_id,
            "official test selectors were not fully collected",
            audit_run_id=audit_run_id,
            source_import_probe=recipe.source_import_probe if recipe else None,
            build_steps=build_steps,
            collection=collection,
            execution=None,
            collected=collected,
            expected=expected,
            audit_path=collection_audit,
        )
    probe_error, probe_path = _probe_source_import(recipe, checkout, test_python)
    if probe_error:
        return _failed_validation_result(
            call_id,
            probe_error,
            audit_run_id=audit_run_id,
            source_import_probe=recipe.source_import_probe if recipe else None,
            build_steps=build_steps,
            collection=collection,
            execution=None,
            collected=collected,
            expected=expected,
            audit_path=collection_audit,
            import_probe_path=probe_path,
        )
    junit = evidence_root / "junit.xml"
    execution = _run_validation_process(
        (*command, f"--junitxml={junit}", "-p", "tracefix_pytest_audit"),
        checkout,
        environment | {
            "TRACEFIX_AUDIT_PATH": str(evidence_root / "execution.audit.json"),
            "TRACEFIX_AUDIT_RUN_ID": f"{audit_run_id}:execution",
            "TRACEFIX_AUDIT_STAGE": "execution",
        },
        evidence_root,
        "execution",
    )
    output = {
        "returncode": execution.returncode,
        "timed_out": execution.timed_out,
        "stdout": _read_text(Path(execution.stdout_path)),
        "stderr": _read_text(Path(execution.stderr_path)),
        "collected_node_ids": list(collected),
        "expected_node_ids": list(expected),
        "import_probe_path": probe_path,
        "build_steps": [step.model_dump(mode="json") for step in build_steps],
        "collection": collection.model_dump(mode="json"),
        "execution": execution.model_dump(mode="json"),
        "audit_path": str(evidence_root / "execution.audit.json"),
        "collection_audit_path": str(evidence_root / "collection.audit.json"),
        "audit_run_id": audit_run_id,
        "source_import_probe": recipe.source_import_probe if recipe else None,
        "junit_path": str(junit),
    }
    return ToolResult(
        call_id=call_id,
        tool_name="run_tests",
        success=execution.returncode == 0 and not execution.timed_out,
        output=output,
        error=None
        if execution.returncode == 0 and not execution.timed_out
        else "pytest execution failed",
        duration_ms=execution.duration_ms,
    )


def _pytest_command(
    test_python: Path, checkout: Path, selectors: tuple[str, ...], evidence_root: Path
) -> tuple[str, ...]:
    """构造不可被 Shell 重解释的 pytest 参数列表。"""
    # ``PYTEST_ADDOPTS`` 已在子进程环境中移除；不要用 ``-o addopts=`` 覆盖
    # 仓库自己的配置，因为历史 pytest 需要其中声明的 ``-p pytester`` 等插件。
    config = next(
        (checkout / name for name in ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg") if (checkout / name).is_file()),
        None,
    )
    if config is None:
        config = evidence_root / "empty-pytest.ini"
        config.write_text("[pytest]\n", encoding="utf-8")
    return (
        str(test_python), "-m", "pytest", "-q", f"--rootdir={checkout}",
        f"--confcutdir={checkout}", "-c", str(config), *selectors,
    )


def _validation_environment(checkout: Path, pythonpath_entries: tuple[Path, ...]) -> dict[str, str]:
    """隔离父项目插件和临时目录，同时让当前副本源码优先被导入。"""
    environment = _isolated_build_environment(checkout)
    roots = [str(path) for path in pythonpath_entries]
    if (checkout / "src").is_dir():
        roots.append(str(checkout / "src"))
    roots.append(str(checkout))
    environment["PYTHONPATH"] = os.pathsep.join(roots)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment.pop("PYTEST_ADDOPTS", None)
    return environment


def _run_validation_process(
    command: tuple[str, ...],
    checkout: Path,
    environment: dict[str, str],
    evidence_root: Path,
    stage: str,
) -> ProcessEvidence:
    """执行一个验证阶段并将完整输出落入当前任务的忽略目录。"""
    started = time.monotonic()
    stdout_path = evidence_root / f"{stage}.stdout.txt"
    stderr_path = evidence_root / f"{stage}.stderr.txt"
    try:
        completed = subprocess.run(
            command, cwd=checkout, env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300, check=False, shell=False,
        )
        returncode, timed_out, stdout, stderr = completed.returncode, False, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        returncode, timed_out = None, True
        stdout = _decode_process_output(exc.stdout)
        stderr = _decode_process_output(exc.stderr)
    except OSError as exc:
        returncode, timed_out, stdout, stderr = None, False, "", str(exc)
    stdout_path.write_text(_sanitize_log(stdout), encoding="utf-8")
    stderr_path.write_text(_sanitize_log(stderr), encoding="utf-8")
    return ProcessEvidence(
        stage=stage, command=command, working_directory=str(checkout), returncode=returncode,
        timed_out=timed_out, duration_ms=(time.monotonic() - started) * 1000,
        stdout_path=str(stdout_path), stderr_path=str(stderr_path),
    )


def _failed_validation_result(
    call_id: str, error: str, *, build_steps: tuple[ProcessEvidence, ...],
    collection: ProcessEvidence | None, execution: ProcessEvidence | None,
    collected: tuple[str, ...] = (), expected: tuple[str, ...] = (),
    audit_path: str | None = None, import_probe_path: str | None = None,
    audit_run_id: str | None = None,
    source_import_probe: str | None = None,
) -> ToolResult:
    """把尚未执行 pytest 的验证失败也包装成完整结构化结果。"""
    build_stdout = "\n".join(_read_text(Path(step.stdout_path)) for step in build_steps)
    build_stderr = "\n".join(_read_text(Path(step.stderr_path)) for step in build_steps)
    collection_stdout = _read_text(Path(collection.stdout_path)) if collection else ""
    collection_stderr = _read_text(Path(collection.stderr_path)) if collection else ""
    output = {
        "returncode": execution.returncode if execution else None,
        "timed_out": execution.timed_out if execution else False,
        "stdout": _read_text(Path(execution.stdout_path)) if execution else collection_stdout or build_stdout,
        "stderr": _read_text(Path(execution.stderr_path)) if execution else collection_stderr or build_stderr,
        "collected_node_ids": list(collected), "expected_node_ids": list(expected),
        "build_steps": [step.model_dump(mode="json") for step in build_steps],
        "collection": collection.model_dump(mode="json") if collection else None,
        "execution": execution.model_dump(mode="json") if execution else None,
        # A collection-only failure has no execution audit.  Reusing its path for
        # both fields makes the verifier compare a collection record to an
        # execution run-id and produces a misleading stage-identity failure.
        "audit_path": audit_path if execution else None,
        "collection_audit_path": audit_path if collection else None,
        "audit_run_id": audit_run_id or call_id, "import_probe_path": import_probe_path,
        "source_import_probe": source_import_probe,
    }
    return ToolResult(call_id=call_id, tool_name="run_tests", success=False, output=output, error=error)


def _decode_process_output(value: bytes | str | None) -> str:
    """统一超时异常中 bytes 与文本输出的编码。"""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


def _read_text(path: Path) -> str:
    """读取已保存的诊断文件；读取失败时返回空文本而不掩盖原始错误。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _probe_source_import(
    recipe: EnvironmentRecipe | None, checkout: Path, test_python: Path
) -> tuple[str | None, str | None]:
    """确认配方指定模块从当前 base/gold 副本导入，而不是环境里的固定源码。

    非 editable 安装仍可能让 Python 优先从 site-packages 导入。这里把工作目录及其
    ``src`` 目录显式置于 ``PYTHONPATH`` 首位，并要求模块的 ``__file__`` 位于本次
    独立 checkout 下；探针失败时不把 pytest 结果当作可信验收证据。
    """
    if recipe is None or not recipe.source_import_probe:
        return None, None
    environment = dict(os.environ)
    entries = [str(checkout), str(checkout / "src")]
    previous = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join([*entries, *([previous] if previous else [])])
    code = (
        "import importlib; module = importlib.import_module(" + repr(recipe.source_import_probe)
        + "); print(getattr(module, '__file__', ''))"
    )
    try:
        completed = subprocess.run(
            [str(test_python), "-c", code],
            cwd=checkout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            shell=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"source import probe failed: {exc}", None
    path_text = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else None
    if completed.returncode != 0 or path_text is None:
        tail = (completed.stderr or completed.stdout)[-500:]
        return f"source import probe failed: {tail}", path_text
    try:
        imported = Path(path_text).resolve()
        imported.relative_to(checkout.resolve())
    except (OSError, ValueError):
        return "source import probe resolved outside the isolated checkout", path_text
    return None, str(imported)


def _run_recipe_build(
    recipe: EnvironmentRecipe | None, checkout: Path, test_python: Path, evidence_root: Path
) -> tuple[ProcessEvidence, ...]:
    """执行配方构建步骤，并为每一步保存命令和完整输出。"""
    if recipe is None:
        return ()
    environment = _isolated_build_environment(checkout)
    steps: list[ProcessEvidence] = []
    for template in recipe.build_commands:
        command = tuple(str(test_python) if item == "{python}" else item for item in template)
        step = _run_validation_process(command, checkout, environment, evidence_root, f"build-{len(steps)}")
        steps.append(step)
        if step.returncode != 0 or step.timed_out:
            break
    return tuple(steps)


_AUDIT_PLUGIN = '''
import json
import os

_records = {
    "format_version": 2,
    "run_id": os.environ.get("TRACEFIX_AUDIT_RUN_ID"),
    "stage": os.environ.get("TRACEFIX_AUDIT_STAGE"),
    "collected_node_ids": [],
    "reports": [],
    "collection_errors": [],
    "imported_source_paths": {},
    "completed": False,
}

def pytest_collection_modifyitems(session, config, items):
    _records["collected_node_ids"] = [item.nodeid for item in items]

def pytest_runtest_logreport(report):
    _records["reports"].append({
        "nodeid": report.nodeid,
        "when": report.when,
        "outcome": report.outcome,
        "wasxfail": bool(getattr(report, "wasxfail", False)),
        "longrepr": str(report.longrepr)[:2000] if report.failed else "",
    })

def pytest_collectreport(report):
    if report.failed:
        _records["collection_errors"].append({
            "nodeid": report.nodeid,
            "longrepr": str(report.longrepr)[:4000],
        })

def pytest_sessionfinish(session, exitstatus):
    import sys
    _records["exitstatus"] = exitstatus
    _records["completed"] = True
    root = str(session.config.rootpath)
    probe = os.environ.get("TRACEFIX_SOURCE_IMPORT_PROBE")
    for name, module in tuple(sys.modules.items()):
        path = getattr(module, "__file__", None)
        is_probed = probe and (name == probe or name.startswith(probe + "."))
        if isinstance(path, str) and (path.startswith(root) or is_probed):
            _records["imported_source_paths"][name] = path
    path = os.environ.get("TRACEFIX_AUDIT_PATH")
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(_records, handle, ensure_ascii=False)
'''


def _write_audit_plugin(checkout: Path) -> None:
    """在临时 checkout 写入 pytest 插件；它不进入待测仓库的提交内容。"""
    (checkout / "tracefix_pytest_audit.py").write_text(_AUDIT_PLUGIN, encoding="utf-8")


def _read_audit(path: Path) -> tuple[tuple[str, ...], str | None]:
    """读取收集阶段插件记录的 node ID；缺失报告由后续分类负责标记。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (), None
    values = payload.get("collected_node_ids", [])
    return tuple(item for item in values if isinstance(item, str)), str(path)


def _audit_complete(audit: object, *, stage: str, run_id: str, returncode: int | None) -> tuple[bool, str | None]:
    """只接受本次阶段完整落盘且退出状态一致的审计记录。"""
    if not isinstance(audit, dict):
        return False, "audit is missing or malformed"
    if audit.get("format_version") != 2:
        return False, "audit format version is unsupported"
    if audit.get("run_id") != run_id or audit.get("stage") != stage:
        return False, "audit does not belong to this validation stage"
    if audit.get("completed") is not True:
        return False, "audit did not record a completed pytest session"
    if audit.get("exitstatus") != returncode:
        return False, "audit exit status differs from pytest process"
    collected = audit.get("collected_node_ids")
    reports = audit.get("reports")
    if not isinstance(collected, list) or not isinstance(reports, list):
        return False, "audit lacks structured node IDs or reports"
    if not isinstance(audit.get("collection_errors"), list):
        return False, "audit lacks collection error records"
    if not all(
        isinstance(item, dict)
        and isinstance(item.get("nodeid"), str)
        and isinstance(item.get("longrepr"), str)
        for item in audit["collection_errors"]
    ):
        return False, "audit collection error is malformed"
    if len(set(collected)) != len(collected):
        return False, "audit contains duplicate collected node IDs"
    if stage == "execution":
        if not collected or not reports:
            return False, "execution audit contains no collected nodes or reports"
        phases: dict[str, set[str]] = {}
        report_keys: set[tuple[str, str]] = set()
        for report in reports:
            if not isinstance(report, dict):
                return False, "audit report is malformed"
            node = report.get("nodeid")
            phase = report.get("when")
            if not isinstance(node, str) or phase not in {"setup", "call", "teardown"}:
                return False, "audit report lacks a test node or phase"
            if node not in collected:
                return False, "audit report references a node outside the collected set"
            if (node, phase) in report_keys:
                return False, "audit contains a duplicate test phase report"
            report_keys.add((node, phase))
            phases.setdefault(node, set()).add(phase)
        if any(phases.get(node) != {"setup", "call", "teardown"} for node in collected):
            return False, "audit is missing setup, call, or teardown evidence"
    return True, None


def _node_ids_from_collect_output(output: str) -> tuple[str, ...]:
    """从 pytest collect-only 标准输出提取 node ID，作为插件文件缺失的后备。"""
    return tuple(
        line.strip()
        for line in output.splitlines()
        if "::" in line and not line.lstrip().startswith("<")
    )


def _isolated_build_environment(checkout: Path) -> dict[str, str]:
    """将构建临时文件限制在副本内，避免系统 Temp 的权限与空间干扰。"""
    temporary = checkout / ".tracefix-build-tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(
        {
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "PIP_CACHE_DIR": str(temporary / "pip-cache"),
            "PIP_NO_CACHE_DIR": "1",
        }
    )
    # 构建步骤不需要模型密钥，避免上游构建脚本或子进程读取运行凭证。
    for name in tuple(environment):
        if name.upper() in {"DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}:
            environment.pop(name, None)
    return environment


def _sanitize_log(text: str) -> str:
    """在日志落盘前移除已知模型密钥，保留可审计的其余诊断。"""
    sanitized = text
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        value = os.getenv(name)
        if value:
            sanitized = sanitized.replace(value, "[REDACTED]")
    return sanitized


def _selectors_collected(expected: tuple[str, ...], collected: tuple[str, ...]) -> bool:
    """允许文件级选择器匹配其收集到的具体测试，但不允许无关替代。"""
    def matches(selector: str, node: str) -> bool:
        normalized = node.replace("\\", "/")
        return (
            normalized == selector
            or normalized.startswith(f"{selector}::")
            # 函数级选择器可以合法展开为多个参数化 node ID。
            or normalized.startswith(f"{selector}[")
        )

    return all(any(matches(selector, node) for node in collected) for selector in expected)


def _pytest_evidence(result: ToolResult, checkout: Path) -> PytestExecutionEvidence:
    """将 JUnit 和进程输出归类，避免把安装或收集失败当作有效复现。"""
    output = result.output if isinstance(result.output, dict) else {}
    stdout = str(output.get("stdout", ""))
    stderr = str(output.get("stderr", ""))
    combined = f"{stdout}\n{stderr}"
    timed_out = bool(output.get("timed_out", False))
    collected = tuple(
        str(item) for item in output.get("collected_node_ids", []) if isinstance(item, str)
    )
    expected = tuple(
        str(item) for item in output.get("expected_node_ids", []) if isinstance(item, str)
    )
    probe_path = output.get("import_probe_path")
    audit_path = output.get("audit_path")
    collection_audit_path = output.get("collection_audit_path")
    audit_run_id = output.get("audit_run_id")
    source_import_probe = output.get("source_import_probe")
    execution_data = output.get("execution")
    collection_data = output.get("collection")
    build_data = output.get("build_steps", [])
    audit = _load_audit(Path(audit_path)) if isinstance(audit_path, str) else {}
    collection_audit = (
        _load_audit(Path(collection_audit_path)) if isinstance(collection_audit_path, str) else {}
    )
    execution_available, execution_diagnostic = _audit_complete(
        audit, stage="execution", run_id=f"{audit_run_id}:execution", returncode=_returncode(result)
    )
    collection_returncode = (
        ProcessEvidence.model_validate(collection_data).returncode
        if isinstance(collection_data, dict)
        else None
    )
    collection_available, collection_diagnostic = _audit_complete(
        collection_audit,
        stage="collection",
        run_id=f"{audit_run_id}:collection",
        returncode=collection_returncode,
    )
    audit_available = execution_available
    import_audit = audit if execution_available else collection_audit
    imported_raw = import_audit.get("imported_source_paths", {})
    imported_source_paths = {
        str(name): str(path)
        for name, path in (imported_raw.items() if isinstance(imported_raw, dict) else ())
        if isinstance(name, str)
        and isinstance(path, str)
    }
    probed_paths = (
        tuple(
            path for name, path in imported_source_paths.items()
            if name == source_import_probe or name.startswith(f"{source_import_probe}.")
        )
        if isinstance(source_import_probe, str) and source_import_probe
        else ()
    )
    source_import_audit_valid = not source_import_probe or bool(probed_paths)
    if probed_paths:
        try:
            source_import_audit_valid = all(
                Path(path).resolve().is_relative_to(checkout.resolve()) for path in probed_paths
            )
        except OSError:
            source_import_audit_valid = False
    collection_error_records = tuple(
        CollectionErrorEvidence.model_validate(item)
        for item in collection_audit.get("collection_errors", [])
        if isinstance(item, dict)
        and isinstance(item.get("nodeid"), str)
        and isinstance(item.get("longrepr"), str)
    )
    collection_errors = tuple(item.longrepr for item in collection_error_records)
    reports = audit.get("reports", []) if isinstance(audit, dict) else []
    executed = tuple(
        report["nodeid"] for report in reports
        if isinstance(report, dict) and report.get("when") == "call" and isinstance(report.get("nodeid"), str)
    )
    skipped = sum(1 for report in reports if isinstance(report, dict) and report.get("outcome") == "skipped")
    xfailed = sum(1 for report in reports if isinstance(report, dict) and report.get("wasxfail") and report.get("outcome") == "skipped")
    xpassed = sum(1 for report in reports if isinstance(report, dict) and report.get("wasxfail") and report.get("outcome") == "passed")
    junit_raw = output.get("junit_path")
    junit = Path(junit_raw) if isinstance(junit_raw, str) else checkout / ".tracefix-junit.xml"
    tests = failures = errors = junit_skipped = 0
    available = junit.is_file()
    if available:
        try:
            root = element_tree.parse(junit).getroot()
            for suite in root.iter("testsuite"):
                tests += int(suite.attrib.get("tests", "0"))
                failures += int(suite.attrib.get("failures", "0"))
                errors += int(suite.attrib.get("errors", "0"))
                junit_skipped += int(suite.attrib.get("skipped", "0"))
        except (OSError, ValueError, element_tree.ParseError):
            available = False
    lowered = combined.casefold()
    if _has_permission_error(combined) or _has_permission_error(result.error or ""):
        status = "permission_error"
    elif _has_network_error(combined):
        status = "network_error"
    elif timed_out:
        status = "timeout"
    elif result.error == "pytest collection failed" or "error collecting" in lowered or "collected 0 items" in lowered:
        status = "collection_error"
    elif "modulenotfounderror" in lowered or "no module named" in lowered or "importerror" in lowered:
        status = "dependency_error"
    elif result.error == "official test selectors were not fully collected":
        status = "selector_mismatch"
    elif result.error and result.error.startswith("recipe build command failed"):
        status = "build_error"
    elif result.error and result.error.startswith("source import probe"):
        status = "source_import_error"
    elif audit_available and not source_import_audit_valid:
        status = "source_import_error"
    elif skipped or xfailed or xpassed or junit_skipped:
        status = "skipped_or_xfailed"
    elif not available or not audit_available:
        # 旧格式结果只留下文本时，仍保留 xfail/xpass 的更具体诊断；其余一律
        # 标为报告缺失，行为资格判断会拒绝它们。
        if result.success and "xfailed" in lowered:
            status = "passed_xfail"
        elif result.success and "xpassed" in lowered:
            status = "passed_xpass"
        else:
            status = "report_missing"
    elif tests == 0:
        status = "no_tests"
    elif failures > 0 and errors == 0:
        status = "assertion_failed"
    elif errors > 0:
        status = "execution_error"
    elif result.success:
        # xfail 的零退出码不能冒充 base 已复现；xpass 也单独保留，以便审查
        # 测试标记是否掩盖了真正的 base/gold 行为差异。
        if "xfailed" in lowered:
            status = "passed_xfail"
        elif "xpassed" in lowered:
            status = "passed_xpass"
        else:
            status = "passed"
    else:
        status = "execution_error"
    return PytestExecutionEvidence(
        status=status,
        returncode=_returncode(result),
        timed_out=timed_out,
        junit_available=available,
        audit_available=audit_available,
        collection_audit_available=collection_available,
        execution_audit_available=execution_available,
        # Collection failure is a complete validation path of its own.  There is
        # deliberately no execution report, so its absent audit must not mask a
        # valid collection audit with an irrelevant diagnostic.
        audit_diagnostic=(execution_diagnostic if execution_data is not None else None)
        or collection_diagnostic,
        source_import_audit_valid=source_import_audit_valid,
        imported_source_paths=imported_source_paths,
        collection_errors=collection_errors,
        collection_error_records=collection_error_records,
        test_count=tests,
        failure_count=failures,
        error_count=errors,
        skipped_count=max(skipped, junit_skipped),
        xfailed_count=xfailed,
        xpassed_count=xpassed,
        collected_node_ids=collected,
        executed_node_ids=executed,
        expected_node_ids=expected,
        import_probe_path=str(probe_path) if probe_path else None,
        diagnostic=result.error,
        output_tail=combined[-2000:],
        build_steps=tuple(ProcessEvidence.model_validate(item) for item in build_data if isinstance(item, dict)),
        collection=ProcessEvidence.model_validate(collection_data) if isinstance(collection_data, dict) else None,
        execution=ProcessEvidence.model_validate(execution_data) if isinstance(execution_data, dict) else None,
        audit_path=audit_path if isinstance(audit_path, str) else None,
        collection_exception_type=(
            _collection_exception(collection_errors[0])[0] if len(collection_errors) == 1 else None
        ),
        collection_exception_module=(
            _collection_exception(collection_errors[0])[1] if len(collection_errors) == 1 else None
        ),
        collection_exception_symbol=(
            _collection_exception(collection_errors[0])[2] if len(collection_errors) == 1 else None
        ),
    )


def _collection_exception(message: str) -> tuple[str | None, str | None, str | None]:
    """从 pytest 的 ImportError 文本提取可审查的异常、模块和符号。"""
    match = re.search(
        r"(?P<type>ImportError): cannot import name ['\"](?P<symbol>[^'\"]+)['\"] from ['\"](?P<module>[^'\"]+)['\"]",
        message,
    )
    if match is None:
        return None, None, None
    return match.group("type"), match.group("module"), match.group("symbol")


def _load_audit(path: Path) -> dict[str, object]:
    """读取 pytest 插件的结构化记录；缺失文件会被报告缺失逻辑识别。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _has_permission_error(message: str) -> bool:
    """识别 Windows 与 pip 的权限拒绝文本。"""
    lowered = message.casefold()
    # pytest 无法写缓存时会给出 PytestCacheWarning，但测试本身仍可能完整执行；
    # 只有非缓存警告的访问拒绝才视为环境权限故障。
    if "pytestcachewarning" in lowered:
        return "permission denied" in lowered and "cache path" not in lowered
    return "permission denied" in lowered or "winerror 5" in lowered or "拒绝访问" in message


def _has_network_error(message: str) -> bool:
    """识别代理、连接和 DNS 导致的非业务测试失败。"""
    lowered = message.casefold()
    return any(token in lowered for token in ("proxyerror", "cannot connect to proxy", "connection refused", "name or service not known"))


class RealPrescreenRunner:
    """逐题执行一次 32k 压缩组，并按预先声明的门槛筛选任务。"""

    def __init__(self, runner: TraceFixRunner | None = None) -> None:
        self.runner = runner or TraceFixRunner()

    def run(self, config: RealExperimentConfig) -> RealPrescreenSummary:
        """运行预筛选；每题后覆盖写 summary，意外中断也能保留已完成结果。"""
        tasks = load_real_issue_tasks(config.tasks_dir, task_ids=config.task_ids)
        experiment_id = (
            f"real-prescreen-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
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
            summary = self._summary(experiment_id, started_at, config, results, summary_path)
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
                    arm is ExperimentArm.TREATMENT if context_enabled is None else context_enabled
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
            agent_completed and source_patch_applied and independent_passed and not changed_tests
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
            f"real-paired-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
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


def _repo_map_aggregate(trials: list[RealTrialResult], arm: ExperimentArm) -> RepoMapArmAggregate:
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
            sum(trial.run.cost_cny_estimate or 0 for trial in selected) if cost_complete else None
        ),
        target_read_count=len(first_steps),
        average_first_target_read_step=(
            sum(first_steps) / len(first_steps) if first_steps else None
        ),
    )


def _eligibility_failures(metrics: RealTrajectoryMetrics, trigger_tokens: int) -> tuple[str, ...]:
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
    return resolve_managed_environment_python(root, task_id)


def _returncode(result: ToolResult) -> int | None:
    """从强类型工具结果中安全提取 pytest 退出码。"""
    if not isinstance(result.output, dict):
        return None
    value = result.output.get("returncode")
    return value if isinstance(value, int) else None


def _git(arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
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
    return result
