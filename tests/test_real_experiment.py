import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tracefix import AgentConfig, AgentStatus, LLMConfig, RunResult
from tracefix.exceptions import BenchmarkError
from tracefix.messages import Message, MessageRole
from tracefix.models import LLMResponse, TokenUsage
from tracefix.p2_protocol import (
    P2BudgetedLLM,
    P2CostLedgerRecord,
    P2FormalRunRequirements,
    P2ProtocolConfig,
    P2SimulationLLM,
    P2TrialRecord,
    build_p2_protocol,
    check_p2_inputs,
    estimated_request_reservation,
    read_completed_trial,
    run_p2_experiment,
    run_p2_simulation,
    write_p2_check,
    write_p2_dry_run,
    write_trial_record,
)
from tracefix.paired import ExperimentArm
from tracefix.provenance import collect_run_provenance
from tracefix.real_benchmark import RealIssueTask
from tracefix.real_experiment import (
    RealExperimentConfig,
    RealPairedExperimentRunner,
    RealPrescreenRunner,
    RealRepoMapPrescreenRunner,
    RealTrajectoryMetrics,
    RealTrialResult,
    _audit_complete,
    _canonical_node_ids,
    _collection_exception,
    _eligibility_failures,
    _git,
    _matches_expected_collection_failure,
    _pytest_evidence,
    _task_python,
    analyze_real_trajectory,
    validate_agent_patch_strict,
    validate_real_task_behavior,
)
from tracefix.real_recipes import EnvironmentRecipe, ExpectedBaseFailure
from tracefix.tools import ToolResult

GOLD = """diff --git a/pkg/a.py b/pkg/a.py
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -1 +1 @@
-VALUE = 0
+VALUE = 1
diff --git a/pkg/b.py b/pkg/b.py
--- a/pkg/b.py
+++ b/pkg/b.py
@@ -1 +1 @@
-ENABLED = False
+ENABLED = True
"""

HIDDEN = """diff --git a/tests/test_hidden.py b/tests/test_hidden.py
new file mode 100644
--- /dev/null
+++ b/tests/test_hidden.py
@@ -0,0 +1,5 @@
+from pkg.a import VALUE
+from pkg.b import ENABLED
+
+def test_fixed():
+    assert VALUE == 1 and ENABLED
"""


def test_real_experiment_default_input_budget_is_350k() -> None:
    """程序化真实实验与 CLI 的 350k 默认值必须一致。"""
    assert RealExperimentConfig().agent_config.max_input_tokens == 350_000


def test_pytest_evidence_marks_xfail_as_non_qualifying_pass(tmp_path: Path) -> None:
    """xfail 的零退出码必须与普通通过区分，避免误判为 base 复现。"""
    (tmp_path / ".tracefix-junit.xml").write_text(
        '<testsuite tests="1" failures="0" errors="0" />', encoding="utf-8"
    )
    result = ToolResult(
        call_id="test-xfail",
        tool_name="run_tests",
        success=True,
        output={"returncode": 0, "stdout": "1 xfailed", "stderr": "", "timed_out": False},
    )

    assert _pytest_evidence(result, tmp_path).status == "passed_xfail"


def test_pytest_evidence_classifies_network_and_permission_failures(tmp_path: Path) -> None:
    """外部代理与 pip 权限错误不能误写成待修复的业务断言失败。"""
    network = ToolResult(
        call_id="network",
        tool_name="run_tests",
        success=False,
        error="pytest execution failed",
        output={"stdout": "requests.exceptions.ProxyError: Cannot connect to proxy", "stderr": ""},
    )
    permission = ToolResult(
        call_id="permission",
        tool_name="run_tests",
        success=False,
        error="recipe build command failed",
        output={"stdout": "", "stderr": "Permission denied: pip-build-tracker"},
    )

    assert _pytest_evidence(network, tmp_path).status == "network_error"
    assert _pytest_evidence(permission, tmp_path).status == "permission_error"


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            ToolResult(
                call_id="timeout",
                tool_name="run_tests",
                success=False,
                error="pytest execution timed out",
                output={"timed_out": True, "stdout": "", "stderr": ""},
            ),
            "timeout",
        ),
        (
            ToolResult(
                call_id="collection",
                tool_name="run_tests",
                success=False,
                error="pytest collection failed",
                output={"stdout": "", "stderr": ""},
            ),
            "collection_error",
        ),
        (
            ToolResult(
                call_id="dependency",
                tool_name="run_tests",
                success=False,
                error="pytest execution failed",
                output={"stdout": "ModuleNotFoundError: No module named 'pluggy'", "stderr": ""},
            ),
            "dependency_error",
        ),
        (
            ToolResult(
                call_id="selector",
                tool_name="run_tests",
                success=False,
                error="official test selectors were not fully collected",
                output={"stdout": "", "stderr": ""},
            ),
            "selector_mismatch",
        ),
        (
            ToolResult(
                call_id="build",
                tool_name="run_tests",
                success=False,
                error="recipe build command failed at step 2",
                output={"stdout": "", "stderr": ""},
            ),
            "build_error",
        ),
        (
            ToolResult(
                call_id="source",
                tool_name="run_tests",
                success=False,
                error="source import probe resolved outside checkout",
                output={"stdout": "", "stderr": ""},
            ),
            "source_import_error",
        ),
    ],
)
def test_pytest_evidence_preserves_operational_failure_categories(
    tmp_path: Path, result: ToolResult, expected: str
) -> None:
    """基础设施失败要保持独立分类，不能退化为报告缺失。"""
    assert _pytest_evidence(result, tmp_path).status == expected


def test_pytest_evidence_rejects_success_without_structured_audit(tmp_path: Path) -> None:
    """JUnit 存在但缺少实际 node ID 审计时，不得把结果当作合格通过。"""
    junit = tmp_path / "junit.xml"
    junit.write_text('<testsuite tests="1" failures="0" errors="0" />', encoding="utf-8")
    result = ToolResult(
        call_id="missing-audit",
        tool_name="run_tests",
        success=True,
        output={"returncode": 0, "junit_path": str(junit), "stdout": "1 passed"},
    )

    assert _pytest_evidence(result, tmp_path).status == "report_missing"


def test_pytest_evidence_rejects_source_loaded_outside_checkout(tmp_path: Path) -> None:
    """单独导入探针通过也不能替代真实 pytest 进程中的源码身份。"""
    junit = tmp_path / "junit.xml"
    junit.write_text('<testsuite tests="1" failures="0" errors="0" />', encoding="utf-8")
    collection = tmp_path / "collection.json"
    execution = tmp_path / "execution.json"
    common = {
        "format_version": 2,
        "collected_node_ids": ["tests/test_x.py::test_x"],
        "collection_errors": [],
        "completed": True,
        "exitstatus": 0,
    }
    collection.write_text(
        json.dumps(common | {"run_id": "run:collection", "stage": "collection", "reports": []}),
        encoding="utf-8",
    )
    reports = [
        {"nodeid": "tests/test_x.py::test_x", "when": phase, "outcome": "passed", "wasxfail": False}
        for phase in ("setup", "call", "teardown")
    ]
    execution.write_text(
        json.dumps(
            common
            | {
                "run_id": "run:execution",
                "stage": "execution",
                "reports": reports,
                "imported_source_paths": {"pkg": str(tmp_path.parent / "site-packages/pkg.py")},
            }
        ),
        encoding="utf-8",
    )
    process = {
        "stage": "execution",
        "command": ["pytest"],
        "working_directory": str(tmp_path),
        "returncode": 0,
        "timed_out": False,
        "duration_ms": 1,
        "stdout_path": str(tmp_path / "out"),
        "stderr_path": str(tmp_path / "err"),
    }
    result = ToolResult(
        call_id="source",
        tool_name="run_tests",
        success=True,
        output={
            "returncode": 0,
            "junit_path": str(junit),
            "audit_path": str(execution),
            "collection_audit_path": str(collection),
            "audit_run_id": "run",
            "source_import_probe": "pkg",
            "collection": process | {"stage": "collection"},
            "execution": process,
        },
    )
    evidence = _pytest_evidence(result, tmp_path)
    assert evidence.status == "source_import_error"
    assert evidence.source_import_audit_valid is False


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"format_version": 1}, "format version"),
        ({"run_id": "stale"}, "does not belong"),
        ({"completed": False}, "completed"),
        ({"exitstatus": 2}, "exit status"),
        ({"reports": None}, "structured node IDs"),
        ({"collection_errors": None}, "collection error"),
        ({"collected_node_ids": []}, "no collected nodes"),
        ({"reports": [{}]}, "lacks a test node"),
        (
            {"reports": [{"nodeid": "tests/test_x.py::test_x", "when": "call"}]},
            "missing setup",
        ),
        (
            {
                "reports": [
                    {"nodeid": "tests/test_x.py::test_x", "when": phase}
                    for phase in ("setup", "call", "teardown", "call")
                ]
            },
            "duplicate test phase",
        ),
        (
            {
                "reports": [
                    {"nodeid": "tests/test_y.py::test_y", "when": phase}
                    for phase in ("setup", "call", "teardown")
                ]
            },
            "outside the collected set",
        ),
    ],
)
def test_execution_audit_fails_closed_for_incomplete_evidence(update, message) -> None:
    """审计身份、结构或测试阶段不完整时必须给出明确拒绝原因。"""
    audit = {
        "format_version": 2,
        "run_id": "run",
        "stage": "execution",
        "completed": True,
        "exitstatus": 0,
        "collected_node_ids": ["tests/test_x.py::test_x"],
        "reports": [
            {"nodeid": "tests/test_x.py::test_x", "when": phase}
            for phase in ("setup", "call", "teardown")
        ],
        "collection_errors": [],
    }
    valid, diagnostic = _audit_complete(
        audit | update, stage="execution", run_id="run", returncode=0
    )
    assert valid is False
    assert message in str(diagnostic)


def test_node_id_normalization_preserves_directory_and_parameter(tmp_path: Path) -> None:
    """两个同名测试文件不能因旧的截断规则而被视为同一个测试。"""
    root = tmp_path / "checkout"
    root.mkdir()
    ids = _canonical_node_ids(
        (
            str(root / "first" / "test_same.py") + "::test_value[param-a]",
            str(root / "second" / "test_same.py") + "::test_value[param-a]",
        ),
        root,
    )

    assert ids == {
        "first/test_same.py::test_value[param-a]",
        "second/test_same.py::test_value[param-a]",
    }
    assert _canonical_node_ids(("tests/test_a.py::test_value[a\\b]",), root) == {
        "tests/test_a.py::test_value[a\\b]"
    }


def test_reviewed_collection_failure_must_match_every_declared_field(tmp_path: Path) -> None:
    """配方不能把任意 ImportError 放进受审查的 base 失败类别。"""
    audit = tmp_path / "collection.audit.json"
    audit.write_text(
        json.dumps(
            {
                "format_version": 2,
                "run_id": "collection:collection",
                "stage": "collection",
                "collected_node_ids": [],
                "reports": [],
                "collection_errors": [
                    {
                        "nodeid": "tests/test_x.py",
                        "longrepr": "ImportError: cannot import name 'NEW_API' from 'pkg.module'",
                    }
                ],
                "completed": True,
                "exitstatus": 2,
            }
        ),
        encoding="utf-8",
    )
    evidence = _pytest_evidence(
        ToolResult(
            call_id="collection",
            tool_name="run_tests",
            success=False,
            error="pytest collection failed",
            output={
                "stdout": "ImportError: cannot import name 'NEW_API' from 'pkg.module'",
                "collection": {
                    "stage": "collection",
                    "command": ["pytest"],
                    "working_directory": str(tmp_path),
                    "returncode": 2,
                    "timed_out": False,
                    "duration_ms": 1,
                    "stdout_path": str(tmp_path / "out"),
                    "stderr_path": str(tmp_path / "err"),
                },
                "collection_audit_path": str(audit),
                "audit_run_id": "collection",
            },
        ),
        tmp_path,
    )
    rule = ExpectedBaseFailure(
        stage="collection",
        exception_type="ImportError",
        module="pkg.module",
        symbol="NEW_API",
        test_entry="tests/test_x.py",
        reason="gold adds the API",
    )

    assert _matches_expected_collection_failure(evidence, rule)
    assert not _matches_expected_collection_failure(
        evidence, rule.model_copy(update={"symbol": "OTHER_API"})
    )
    assert not _matches_expected_collection_failure(
        evidence, rule.model_copy(update={"test_entry": "tests/other.py"})
    )
    assert _collection_exception("ImportError: cannot import name 'NEW_API' from 'pkg.module'") == (
        "ImportError",
        "pkg.module",
        "NEW_API",
    )

    payload = json.loads(audit.read_text(encoding="utf-8"))
    payload["collection_errors"].append(
        {"nodeid": "tests/test_y.py", "longrepr": "RuntimeError: unrelated"}
    )
    audit.write_text(json.dumps(payload), encoding="utf-8")
    mixed = _pytest_evidence(
        ToolResult(
            call_id="collection",
            tool_name="run_tests",
            success=False,
            error="pytest collection failed",
            output={
                "stdout": "ImportError: cannot import name 'NEW_API' from 'pkg.module'",
                "collection": evidence.collection.model_dump(mode="json"),
                "collection_audit_path": str(audit),
                "audit_run_id": "collection",
            },
        ),
        tmp_path,
    )
    assert not _matches_expected_collection_failure(mixed, rule)


def _run_git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _digest(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fixture(tmp_path: Path) -> tuple[RealIssueTask, Path]:
    source = tmp_path / "source"
    (source / "pkg").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "pkg" / "a.py").write_text("VALUE = 0\n", encoding="utf-8")
    (source / "pkg" / "b.py").write_text("ENABLED = False\n", encoding="utf-8")
    for name in ("c.py", "d.py", "e.py"):
        (source / "pkg" / name).write_text("# context\n", encoding="utf-8")
    _run_git(source, "init", "--quiet")
    _run_git(source, "add", "--all")
    _run_git(
        source,
        "-c",
        "user.name=TraceFix Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "base",
    )
    commit = _run_git(source, "rev-parse", "HEAD")
    task_dir = tmp_path / "tasks" / "owner__repo-1"
    task_dir.mkdir(parents=True)
    (task_dir / "problem.md").write_text("Fix both flags.\n", encoding="utf-8")
    (task_dir / "gold.patch").write_text(GOLD, encoding="utf-8")
    (task_dir / "test.patch").write_text(HIDDEN, encoding="utf-8")
    manifest = {
        "id": "owner__repo-1",
        "title": "Two-file issue",
        "repo": "owner/repo",
        "repo_url": "https://github.com/owner/repo.git",
        "issue_url": "https://github.com/owner/repo/issues/1",
        "base_commit": commit,
        "environment_setup_commit": commit,
        "upstream_version": "1.0",
        "issue_created_at": "2024-01-01T00:00:00Z",
        "problem_statement_kind": "curated_excerpt",
        "fail_to_pass": ["tests/test_hidden.py"],
        "test_command": "pytest -q -p no:cacheprovider tests/test_hidden.py",
        "pass_to_pass_count": 0,
        "expected_source_files": ["pkg/a.py", "pkg/b.py"],
        "expected_test_files": ["tests/test_hidden.py"],
        "related_context_files": [
            "pkg/a.py",
            "pkg/b.py",
            "pkg/c.py",
            "pkg/d.py",
            "pkg/e.py",
        ],
        "hashes": {
            "problem_statement": _digest(task_dir / "problem.md"),
            "gold_patch": _digest(task_dir / "gold.patch"),
            "test_patch": _digest(task_dir / "test.patch"),
        },
    }
    (task_dir / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    return RealIssueTask.load(task_dir), source


def _trace(path: Path, *, eligible: bool = True) -> None:
    events = [
        {
            "event_type": "context_prepared",
            "step": 2,
            "payload": {
                "estimated_tokens_before": 33_000 if eligible else 2_000,
                "estimated_tokens_after": 12_000 if eligible else 2_000,
            },
        },
        {
            "event_type": "context_compacted",
            "step": 2,
            "payload": {"batches_compacted": 2 if eligible else 0},
        },
        {"event_type": "model_request_view", "step": 2, "payload": {}},
    ]
    for name in ("a.py", "b.py", "c.py", "d.py", "e.py"):
        events.append(
            {
                "event_type": "tool_called",
                "step": 1,
                "payload": {"call": {"name": "read_file", "arguments": {"path": f"pkg/{name}"}}},
            }
        )
    events.extend(
        [
            {
                "event_type": "tool_called",
                "step": 2,
                "payload": {"call": {"name": "apply_patch", "arguments": {}}},
            },
            {
                "event_type": "tool_called",
                "step": 3,
                "payload": {"call": {"name": "run_tests", "arguments": {}}},
            },
            {
                "event_type": "tool_returned",
                "step": 3,
                "payload": {"result": {"tool_name": "run_tests", "success": True}},
            },
        ]
    )
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


class _FakeRunner:
    def run(self, config) -> RunResult:
        run_dir = config.output_dir / "fake-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        trace = run_dir / "trajectory.jsonl"
        diff = run_dir / "patch.diff"
        result_path = run_dir / "result.json"
        _trace(trace)
        diff.write_text(GOLD, encoding="utf-8")
        now = datetime.now(UTC)
        return RunResult(
            run_id="fake-run",
            source_repo=str(config.repo),
            source_commit=_run_git(config.repo, "rev-parse", "HEAD"),
            workspace=str(run_dir / "workspace"),
            model_name=config.model_name,
            status=AgentStatus.COMPLETED,
            stop_reason="assistant_final",
            final_output="done",
            step_count=3,
            input_tokens=100,
            output_tokens=20,
            test_runs=1,
            cost_usd=0.01,
            cost_complete=True,
            usd_cny_rate=config.usd_cny_rate,
            cost_cny_estimate=0.072,
            started_at=now,
            finished_at=now,
            duration_seconds=1,
            changed_files=("pkg/a.py", "pkg/b.py"),
            trace_path=str(trace),
            diff_path=str(diff),
            result_path=str(result_path),
            agent_config=config.agent_config,
            provenance=collect_run_provenance(config.task, LLMConfig(model_name=config.model_name)),
        )


def test_behavior_validation_proves_initial_fail_and_gold_pass(tmp_path) -> None:
    task, source = _fixture(tmp_path)
    result = validate_real_task_behavior(
        task,
        source=source,
        test_python=Path(sys.executable),
        output_dir=tmp_path / "behavior",
    )

    assert result.initial_hidden_failed is True
    assert result.gold_hidden_passed is True
    assert result.initial_evidence.status == "assertion_failed"
    assert result.gold_evidence.status == "passed"
    assert result.eligible_for_llm_prescreen is True
    assert result.initial_returncode != 0
    assert result.gold_returncode == 0
    assert result.test_environment.python_version
    assert result.dependency_drift_detected is False
    assert (
        result.environment_before.fingerprint_sha256
        == result.environment_after_gold.fingerprint_sha256
    )


def test_behavior_validation_records_isolated_source_import_probe(tmp_path: Path) -> None:
    """base/gold 的模块导入路径必须指向各自工作副本，而不是 site-packages。"""
    task, source = _fixture(tmp_path)
    result = validate_real_task_behavior(
        task,
        source=source,
        test_python=Path(sys.executable),
        output_dir=tmp_path / "behavior-probe",
        recipe=EnvironmentRecipe(task_id=task.id, source_import_probe="pkg.a"),
    )

    assert result.initial_evidence.import_probe_path is not None
    assert result.gold_evidence.import_probe_path is not None
    assert "owner__repo-1-initial" in result.initial_evidence.import_probe_path
    assert "owner__repo-1-gold" in result.gold_evidence.import_probe_path


def test_strict_agent_patch_validation_rejects_empty_patch(tmp_path: Path) -> None:
    task, source = _fixture(tmp_path)
    patch = tmp_path / "empty.diff"
    patch.write_text("", encoding="utf-8")

    result = validate_agent_patch_strict(
        task, source=source, agent_patch=patch, test_python=Path(sys.executable),
        output_dir=tmp_path / "agent-validation",
    )

    assert result.patch_applied is False
    assert result.eligible is False
    assert result.reason == "empty_agent_patch"


def test_strict_agent_patch_validation_accepts_complete_hidden_evidence(tmp_path: Path) -> None:
    task, source = _fixture(tmp_path)
    patch = tmp_path / "gold.diff"
    patch.write_text(GOLD, encoding="utf-8")

    result = validate_agent_patch_strict(
        task, source=source, agent_patch=patch, test_python=Path(sys.executable),
        output_dir=tmp_path / "strict-agent-validation",
        recipe=EnvironmentRecipe(task_id=task.id, source_import_probe="pkg.a"),
    )

    assert result.patch_applied is True
    assert result.eligible is True
    assert result.evidence is not None
    assert result.evidence.source_import_audit_valid is True


def test_strict_agent_patch_validation_rejects_untracked_test_file(tmp_path: Path) -> None:
    """Agent 新建未跟踪测试也属于验收入口篡改，不能被 git diff 漏掉。"""
    task, source = _fixture(tmp_path)
    patch = tmp_path / "new-test.diff"
    patch.write_text(
        "diff --git a/tests/test_agent_bypass.py b/tests/test_agent_bypass.py\n"
        "new file mode 100644\n--- /dev/null\n+++ b/tests/test_agent_bypass.py\n"
        "@@ -0,0 +1,2 @@\n+def test_bypass():\n+    assert True\n",
        encoding="utf-8",
    )
    result = validate_agent_patch_strict(
        task, source=source, agent_patch=patch, test_python=Path(sys.executable),
        output_dir=tmp_path / "strict-agent-untracked",
    )
    assert result.patch_applied is True
    assert result.eligible is False
    assert result.reason == "agent_modified_test_or_pytest_configuration"


def test_prescreen_runs_hidden_verification_and_applies_entry_gate(tmp_path, monkeypatch) -> None:
    task, source = _fixture(tmp_path)
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source.rename(source_root / task.id)
    monkeypatch.setattr("tracefix.real_experiment._task_python", lambda *_: Path(sys.executable))

    summary = RealPrescreenRunner(_FakeRunner()).run(
        RealExperimentConfig(
            tasks_dir=task.task_dir.parent,
            source_root=source_root,
            test_env_root=tmp_path / "envs",
            output_dir=tmp_path / "runs",
            task_ids=(task.id,),
        )
    )

    trial = summary.results[0]
    assert trial.agent_completed is True
    assert trial.agent_selected_tests_passed is True
    assert trial.public_tests_passed is None
    assert trial.independent_tests_passed is True
    assert trial.source_patch_applied is True
    assert trial.tests_modified is False
    assert trial.resolved is True
    assert trial.eligible_for_paired is True
    assert summary.eligible_task_ids == (task.id,)
    assert summary.eligible_for_formal_experiment is False


def test_trajectory_reports_missing_gate_reasons(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    _trace(path, eligible=False)
    metrics = analyze_real_trajectory(
        path,
        related_files=("pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/d.py", "pkg/e.py"),
    )
    failures = _eligibility_failures(metrics, 32_000)

    assert "no_history_compaction" in failures
    assert "request_never_reached_trigger" in failures
    assert metrics.request_views_recorded == 1


def test_trajectory_records_first_read_of_evaluator_only_target_file(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    _trace(path)

    metrics = analyze_real_trajectory(
        path,
        related_files=("pkg/a.py", "pkg/b.py"),
        target_files=("pkg/b.py",),
    )

    assert metrics.first_target_read_path == "pkg/b.py"
    assert metrics.first_target_read_step == 1
    assert metrics.first_target_read_tool_call == 2
    assert metrics.first_patch_step == 2
    assert metrics.first_test_step == 3


def test_gate_requires_files_loop_and_request_view() -> None:
    metrics = RealTrajectoryMetrics(max_estimated_tokens_before=32_000)
    failures = _eligibility_failures(metrics, 32_000)

    assert "fewer_than_five_meaningful_files_read" in failures
    assert "no_test_and_patch_feedback_loop" in failures
    assert "request_views_missing" in failures


def test_paired_runner_rejects_fewer_than_three_eligible_tasks(tmp_path) -> None:
    with pytest.raises(BenchmarkError, match="3 eligible"):
        RealPairedExperimentRunner().run_paired(
            RealExperimentConfig(output_dir=tmp_path),
            eligible_task_ids=("only-one",),
        )


def test_paired_runner_uses_ct_tc_ct_order_and_persists_summary(tmp_path, monkeypatch) -> None:
    task, source = _fixture(tmp_path)
    config = RealExperimentConfig(output_dir=tmp_path / "runs")
    fake_run = _FakeRunner().run(
        type(
            "Config",
            (),
            {
                "output_dir": tmp_path / "fake",
                "repo": source,
                "model_name": config.model_name,
                "task": task.problem_statement,
                "usd_cny_rate": config.usd_cny_rate,
                "agent_config": AgentConfig(),
            },
        )()
    )
    tasks = tuple(task.model_copy(update={"id": f"task-{index}"}) for index in range(3))
    monkeypatch.setattr(
        "tracefix.real_experiment.load_real_issue_tasks", lambda *args, **kwargs: tasks
    )
    runner = RealPairedExperimentRunner()

    def fake_trial(config, task, arm, sequence, repetition, *paths):
        return RealTrialResult(
            sequence=sequence,
            task_id=task.id,
            arm=arm,
            repetition=repetition,
            run=fake_run,
            agent_completed=True,
            independent_tests_passed=True,
            tests_modified=False,
            resolved=True,
            trajectory=RealTrajectoryMetrics(),
            eligible_for_paired=True,
        )

    monkeypatch.setattr(runner, "_run_trial", fake_trial)
    summary = runner.run_paired(
        config,
        eligible_task_ids=tuple(item.id for item in tasks),
        repetitions=3,
    )

    assert len(summary.trials) == 18
    assert [trial.arm for trial in summary.trials[:2]] == [
        ExperimentArm.CONTROL,
        ExperimentArm.TREATMENT,
    ]
    assert [trial.arm for trial in summary.trials[6:8]] == [
        ExperimentArm.TREATMENT,
        ExperimentArm.CONTROL,
    ]
    assert Path(summary.summary_path).is_file()


def test_p2_protocol_fixes_whole_system_60_trial_schedule_and_offline_gate(
    tmp_path, monkeypatch
) -> None:
    """P2 固定三轮 C/T、T/C、C/T；未填写商业参数只能做零费用演练。"""
    task, _ = _fixture(tmp_path)
    ids = tuple(f"task-{index}" for index in range(10))
    tasks = tuple(task.model_copy(update={"id": task_id}) for task_id in ids)
    recipes = {
        task_id: EnvironmentRecipe(task_id=task_id)
        for task_id in ids
    }
    monkeypatch.setattr("tracefix.p2_protocol.P1_QUALIFIED_TASK_IDS", ids)
    monkeypatch.setattr("tracefix.p2_protocol.COLLECTION_FAILURE_TASK_IDS", ids[-2:])
    monkeypatch.setattr("tracefix.p2_protocol.load_real_issue_tasks", lambda *args, **kwargs: tasks)
    monkeypatch.setattr("tracefix.p2_protocol.load_environment_recipes", lambda *_: recipes)
    monkeypatch.setattr("tracefix.p2_protocol._git_commit", lambda *_: "a" * 40)
    config = P2ProtocolConfig(output_dir=tmp_path / "runs")

    protocol = build_p2_protocol(config)

    assert len(protocol.schedule) == 60
    assert protocol.formal_ready is False
    assert protocol.formal_missing == (
        "model_name", "provider", "pricing_source", "total_cost_cap_usd",
        "input_cost_per_million_usd", "output_cost_per_million_usd",
    )
    assert [item.arm for item in protocol.schedule[:2]] == [
        ExperimentArm.CONTROL, ExperimentArm.TREATMENT
    ]
    assert [item.arm for item in protocol.schedule[20:22]] == [
        ExperimentArm.TREATMENT, ExperimentArm.CONTROL
    ]
    assert protocol.schedule[0].token_optimization_enabled is False
    assert protocol.schedule[1].context_compaction_enabled is True
    path = write_p2_dry_run(config)
    assert path.is_file()
    assert "p2_whole_system_ct_protocol" in path.read_text(encoding="utf-8")


def test_p2_protocol_accepts_complete_commercial_requirements(tmp_path, monkeypatch) -> None:
    task, source = _fixture(tmp_path)
    ids = tuple(f"task-{index}" for index in range(10))
    tasks = tuple(task.model_copy(update={"id": task_id}) for task_id in ids)
    recipes = {task_id: EnvironmentRecipe(task_id=task_id) for task_id in ids}
    monkeypatch.setattr("tracefix.p2_protocol.P1_QUALIFIED_TASK_IDS", ids)
    monkeypatch.setattr("tracefix.p2_protocol.COLLECTION_FAILURE_TASK_IDS", ids[-2:])
    monkeypatch.setattr("tracefix.p2_protocol.load_real_issue_tasks", lambda *args, **kwargs: tasks)
    monkeypatch.setattr("tracefix.p2_protocol.load_environment_recipes", lambda *_: recipes)

    protocol = build_p2_protocol(
        P2ProtocolConfig(
            formal=P2FormalRunRequirements(
                model_name="provider/model", provider="provider",
                pricing_source="https://example.invalid/pricing", total_cost_cap_usd=1.0,
                input_cost_per_million_usd=1.0, output_cost_per_million_usd=2.0,
            )
        ),
        repository_root=source,
    )

    assert protocol.formal_ready is True
    assert protocol.formal_missing == ()
    assert protocol.offline_only is False
    assert protocol.code_commit == _run_git(source, "rev-parse", "HEAD")


def test_p2_trial_records_resume_only_completed_and_reserve_cost(tmp_path: Path) -> None:
    record = P2TrialRecord(
        sequence=1, task_id="task", arm=ExperimentArm.CONTROL, repetition=1,
        mode="simulation", status="verification_complete", input_tokens=10,
        output_tokens=5, cost_usd=0,
    )
    path = tmp_path / "trial.json"
    write_trial_record(path, record)
    resumed = read_completed_trial(path)
    assert resumed is not None and resumed.resumed is True
    requirements = P2FormalRunRequirements(
        model_name="provider/model", provider="provider", pricing_source="source",
        total_cost_cap_usd=1, input_cost_per_million_usd=2, output_cost_per_million_usd=4,
    )
    assert (
        estimated_request_reservation(
            requirements, input_tokens=1_000_000, output_tokens=500_000
        )
        == 4
    )
    write_trial_record(path, record.model_copy(update={"status": "request_uncertain"}))
    with pytest.raises(BenchmarkError, match="uncertain"):
        read_completed_trial(path)


def test_p2_budgeted_llm_reserves_reconciles_and_freezes_uncertain_request(tmp_path) -> None:
    requirements = P2FormalRunRequirements(
        model_name="provider/model", provider="provider", pricing_source="source",
        total_cost_cap_usd=10, input_cost_per_million_usd=2,
        output_cost_per_million_usd=4,
    )
    ledger = tmp_path / "ledger.json"
    llm = P2BudgetedLLM(
        LLMConfig(model_name="provider/model", max_output_tokens=100, max_retries=0),
        ledger_path=ledger, formal=requirements, input_upper_bound=1000,
    )

    class Delegate:
        def complete(self, messages, tools=()):
            return LLMResponse(
                message=Message(role=MessageRole.ASSISTANT, content="done"),
                usage=TokenUsage(input_tokens=100, output_tokens=20, total_tokens=120),
                model_name="provider/model",
            )

    llm._delegate = Delegate()
    response = llm.complete(())
    saved = P2CostLedgerRecord.model_validate_json(ledger.read_text(encoding="utf-8"))
    assert response.usage.cost_usd == pytest.approx(0.00028)
    assert saved.spent_usd == pytest.approx(0.00028)
    assert saved.reserved_usd == 0 and saved.uncertain_request is False

    class Uncertain:
        def complete(self, messages, tools=()):
            raise RuntimeError("connection lost after send")

    llm._delegate = Uncertain()
    with pytest.raises(RuntimeError, match="connection lost"):
        llm.complete(())
    with pytest.raises(BenchmarkError, match="uncertain"):
        llm.complete(())

    too_small = requirements.model_copy(update={"total_cost_cap_usd": 0.0001})
    limited = P2BudgetedLLM(
        LLMConfig(model_name="provider/model", max_output_tokens=100, max_retries=0),
        ledger_path=tmp_path / "limited.json", formal=too_small, input_upper_bound=1000,
    )
    with pytest.raises(BenchmarkError, match="cost cap"):
        limited.complete(())

    mismatched_path = tmp_path / "mismatched.json"
    mismatched_path.write_text(
        P2CostLedgerRecord(cap_usd=1).model_dump_json(), encoding="utf-8"
    )
    mismatch = P2BudgetedLLM(
        LLMConfig(model_name="provider/model", max_output_tokens=100, max_retries=0),
        ledger_path=mismatched_path, formal=requirements, input_upper_bound=1000,
    )
    with pytest.raises(BenchmarkError, match="does not match"):
        mismatch.complete(())


def test_p2_simulation_model_emits_supported_nonempty_git_patch() -> None:
    response = P2SimulationLLM(LLMConfig(model_name="simulation")).complete(())
    patch = response.message.tool_calls[0].arguments["patch"]
    assert isinstance(patch, str)
    assert patch.startswith("diff --git ")
    assert "--- /dev/null" in patch


def test_p2_formal_mode_rejects_missing_commercial_parameters(tmp_path) -> None:
    with pytest.raises(BenchmarkError, match="commercial parameters"):
        run_p2_experiment(
            P2ProtocolConfig(), experiment_dir=tmp_path / "formal", mode="formal"
        )
    with pytest.raises(BenchmarkError, match="unknown"):
        run_p2_experiment(
            P2ProtocolConfig(), experiment_dir=tmp_path / "unknown", mode="invalid"
        )


def test_p2_simulation_completes_and_resumes_all_trials(tmp_path, monkeypatch) -> None:
    task, source = _fixture(tmp_path)
    ids = tuple(f"task-{index}" for index in range(10))
    tasks = tuple(task.model_copy(update={"id": task_id}) for task_id in ids)
    recipes = {task_id: EnvironmentRecipe(task_id=task_id) for task_id in ids}
    monkeypatch.setattr("tracefix.p2_protocol.P1_QUALIFIED_TASK_IDS", ids)
    monkeypatch.setattr("tracefix.p2_protocol.COLLECTION_FAILURE_TASK_IDS", ids[-2:])
    monkeypatch.setattr("tracefix.p2_protocol.load_real_issue_tasks", lambda *args, **kwargs: tasks)
    monkeypatch.setattr("tracefix.p2_protocol.load_environment_recipes", lambda *_: recipes)
    monkeypatch.setattr("tracefix.p2_protocol._git_commit", lambda *_: "c" * 40)
    sources = tmp_path / "sources"
    sources.mkdir()
    for task_id in ids:
        _run_git(tmp_path, "clone", "--quiet", str(source), str(sources / task_id))
    monkeypatch.setattr(
        "tracefix.p2_protocol.resolve_managed_environment_python",
        lambda *_: Path(sys.executable),
    )
    class Verification:
        eligible = False

        def model_dump_json(self, **kwargs):
            return '{"eligible": false}'
    monkeypatch.setattr(
        "tracefix.p2_protocol.validate_agent_patch_strict",
        lambda *args, **kwargs: Verification(),
    )
    # 此单测验证 60 次状态机与恢复，不重复运行真实 Agent 子进程；真实闭环由
    # P2 离线演练工件覆盖。
    class SimulationRunner:
        def run(self, run_config):
            return _FakeRunner().run(run_config).model_copy(update={"cost_usd": 0})

    monkeypatch.setattr("tracefix.p2_protocol.TraceFixRunner", lambda *_: SimulationRunner())
    root = tmp_path / "simulation"
    config = P2ProtocolConfig(source_root=sources)
    first = run_p2_simulation(config, experiment_dir=root)
    resumed = run_p2_simulation(config, experiment_dir=root)
    assert first.completed_count == 60 and first.resumed_count == 0
    assert resumed.completed_count == 60 and resumed.resumed_count == 60
    assert resumed.cost_usd == 0
    protocol_path = root / "protocol.json"
    payload = json.loads(protocol_path.read_text(encoding="utf-8"))
    payload["code_commit"] = "changed"
    protocol_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BenchmarkError, match="identity does not match"):
        run_p2_simulation(config, experiment_dir=root)


def test_p2_input_check_writes_snapshot_and_rejects_dirty_source(tmp_path, monkeypatch) -> None:
    task, source = _fixture(tmp_path)
    monkeypatch.setattr("tracefix.p2_protocol.P1_QUALIFIED_TASK_IDS", (task.id,))
    monkeypatch.setattr("tracefix.p2_protocol.COLLECTION_FAILURE_TASK_IDS", ())
    monkeypatch.setattr(
        "tracefix.p2_protocol.load_real_issue_tasks", lambda *args, **kwargs: (task,)
    )
    monkeypatch.setattr(
        "tracefix.p2_protocol.load_environment_recipes",
        lambda *_: {task.id: EnvironmentRecipe(task_id=task.id)},
    )
    monkeypatch.setattr(
        "tracefix.p2_protocol.resolve_managed_environment_python",
        lambda *_: Path(sys.executable),
    )
    monkeypatch.setattr("tracefix.p2_protocol._git_commit", lambda *_: "d" * 40)
    sources = tmp_path / "sources"
    sources.mkdir()
    _run_git(tmp_path, "clone", "--quiet", str(source), str(sources / task.id))
    config = P2ProtocolConfig(source_root=sources, output_dir=tmp_path / "runs")
    assert write_p2_check(config).is_file()
    formal = P2FormalRunRequirements(
        model_name="provider/model", provider="provider", pricing_source="source",
        total_cost_cap_usd=1, input_cost_per_million_usd=1,
        output_cost_per_million_usd=1,
    )
    monkeypatch.setattr("tracefix.p2_protocol._tracked_diff", lambda *_: b"diff")
    with pytest.raises(BenchmarkError, match="clean tracked worktree"):
        check_p2_inputs(config.model_copy(update={"formal": formal}))
    (sources / task.id / "dirty.txt").write_text("x", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="not clean"):
        check_p2_inputs(config)


def test_p2_simulation_rejects_existing_lock(tmp_path, monkeypatch) -> None:
    task, _ = _fixture(tmp_path)
    monkeypatch.setattr("tracefix.p2_protocol.P1_QUALIFIED_TASK_IDS", (task.id,))
    monkeypatch.setattr("tracefix.p2_protocol.COLLECTION_FAILURE_TASK_IDS", ())
    monkeypatch.setattr(
        "tracefix.p2_protocol.load_real_issue_tasks", lambda *args, **kwargs: (task,)
    )
    monkeypatch.setattr(
        "tracefix.p2_protocol.load_environment_recipes",
        lambda *_: {task.id: EnvironmentRecipe(task_id=task.id)},
    )
    monkeypatch.setattr("tracefix.p2_protocol._git_commit", lambda *_: "e" * 40)
    monkeypatch.setattr("tracefix.p2_protocol.check_p2_inputs", lambda *args, **kwargs: type(
        "Check", (), {"protocol": build_p2_protocol(P2ProtocolConfig())}
    )())
    root = tmp_path / "simulation"
    root.mkdir()
    (root / ".p2-run.lock").write_text("other", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="already running"):
        run_p2_simulation(P2ProtocolConfig(), experiment_dir=root)


def test_p2_simulation_records_missing_agent_patch_as_infrastructure_error(
    tmp_path, monkeypatch
) -> None:
    task, source = _fixture(tmp_path)
    monkeypatch.setattr("tracefix.p2_protocol.P1_QUALIFIED_TASK_IDS", (task.id,))
    monkeypatch.setattr("tracefix.p2_protocol.COLLECTION_FAILURE_TASK_IDS", ())
    monkeypatch.setattr(
        "tracefix.p2_protocol.load_real_issue_tasks", lambda *args, **kwargs: (task,)
    )
    monkeypatch.setattr(
        "tracefix.p2_protocol.load_environment_recipes",
        lambda *_: {task.id: EnvironmentRecipe(task_id=task.id)},
    )
    monkeypatch.setattr("tracefix.p2_protocol._git_commit", lambda *_: "f" * 40)
    monkeypatch.setattr(
        "tracefix.p2_protocol.resolve_managed_environment_python",
        lambda *_: Path(sys.executable),
    )
    protocol = build_p2_protocol(P2ProtocolConfig())
    monkeypatch.setattr("tracefix.p2_protocol.check_p2_inputs", lambda *args, **kwargs: type(
        "Check", (), {"protocol": protocol}
    )())
    class NoPatchRunner:
        def run(self, run_config):
            return _FakeRunner().run(run_config).model_copy(
                update={"diff_path": None, "cost_usd": 0}
            )

    monkeypatch.setattr("tracefix.p2_protocol.TraceFixRunner", lambda *_: NoPatchRunner())
    sources = tmp_path / "sources"
    sources.mkdir()
    _run_git(tmp_path, "clone", "--quiet", str(source), str(sources / task.id))
    summary = run_p2_simulation(
        P2ProtocolConfig(source_root=sources), experiment_dir=tmp_path / "simulation"
    )
    assert summary.completed_count == 0
    assert {item.status for item in summary.results} == {"infrastructure_error"}


def test_p2_protocol_rejects_changed_repetition_and_missing_recipe(tmp_path, monkeypatch) -> None:
    task, source = _fixture(tmp_path)
    ids = tuple(f"task-{index}" for index in range(10))
    tasks = tuple(task.model_copy(update={"id": task_id}) for task_id in ids)
    monkeypatch.setattr("tracefix.p2_protocol.P1_QUALIFIED_TASK_IDS", ids)
    monkeypatch.setattr("tracefix.p2_protocol.load_real_issue_tasks", lambda *args, **kwargs: tasks)
    monkeypatch.setattr(
        "tracefix.p2_protocol.load_environment_recipes",
        lambda *_: {task_id: EnvironmentRecipe(task_id=task_id) for task_id in ids[:-1]},
    )

    with pytest.raises(ValueError, match="less than or equal"):
        P2ProtocolConfig(repetitions=4)
    with pytest.raises(BenchmarkError, match="missing environment recipes"):
        build_p2_protocol(P2ProtocolConfig(), repository_root=source)
    monkeypatch.setattr(
        "tracefix.p2_protocol.P1_QUALIFIED_TASK_IDS", (*ids, "task-missing")
    )
    with pytest.raises(BenchmarkError, match="task set is incomplete"):
        build_p2_protocol(P2ProtocolConfig(), repository_root=source)


def test_repo_map_prescreen_alternates_arms_and_keeps_context_policy(tmp_path, monkeypatch) -> None:
    """该预筛选的唯一变量是 Repo Map，不能意外关闭 32k 压缩。"""
    task, source = _fixture(tmp_path)
    config = RealExperimentConfig(output_dir=tmp_path / "runs")
    fake_run = _FakeRunner().run(
        type(
            "Config",
            (),
            {
                "output_dir": tmp_path / "fake",
                "repo": source,
                "model_name": config.model_name,
                "task": task.problem_statement,
                "usd_cny_rate": config.usd_cny_rate,
                "agent_config": AgentConfig(),
            },
        )()
    )
    tasks = tuple(task.model_copy(update={"id": f"task-{index}"}) for index in range(3))
    monkeypatch.setattr(
        "tracefix.real_experiment.load_real_issue_tasks", lambda *args, **kwargs: tasks
    )
    runner = RealRepoMapPrescreenRunner()
    seen = []

    def fake_trial(config, task, arm, sequence, repetition, *paths, **kwargs):
        seen.append((task.id, arm, kwargs["context_enabled"], kwargs["repo_map_enabled"]))
        return RealTrialResult(
            sequence=sequence,
            task_id=task.id,
            arm=arm,
            repo_map_enabled=kwargs["repo_map_enabled"],
            repetition=repetition,
            run=fake_run,
            agent_completed=True,
            public_tests_passed=True,
            independent_tests_passed=True,
            tests_modified=False,
            resolved=True,
            trajectory=RealTrajectoryMetrics(first_target_read_step=2),
            eligible_for_paired=True,
        )

    monkeypatch.setattr(runner, "_run_trial", fake_trial)
    summary = runner.run(config)

    assert [entry[1] for entry in seen] == [
        ExperimentArm.CONTROL,
        ExperimentArm.TREATMENT,
        ExperimentArm.TREATMENT,
        ExperimentArm.CONTROL,
        ExperimentArm.CONTROL,
        ExperimentArm.TREATMENT,
    ]
    assert all(entry[2] is config.agent_config.context.enabled for entry in seen)
    assert [entry[3] for entry in seen] == [False, True, True, False, False, True]
    assert summary.control.resolved_count == 3
    assert summary.treatment.average_first_target_read_step == 2
    assert Path(summary.summary_path).is_file()


def test_task_python_and_git_failures_are_explicit(tmp_path) -> None:
    with pytest.raises(BenchmarkError, match="interpreter"):
        _task_python(tmp_path, "missing")
    with pytest.raises(BenchmarkError, match="git command"):
        _git(["rev-parse", "--verify", "missing"], tmp_path)
