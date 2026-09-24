import hashlib
import json
from datetime import UTC, datetime

import pytest

from tracefix.ablation import summarize_ablation
from tracefix.exceptions import BenchmarkError
from tracefix.p2_protocol import (
    P2ProtocolRecord,
    P2QualificationRecord,
    P2TrialRecord,
    _schedule,
    _verification_failure_kind,
    diagnose_p2_experiment,
    summarize_p2_experiment,
    write_p2_summary,
)


@pytest.fixture
def reporting_experiment(tmp_path):
    ids = tuple(f"task-{i}" for i in range(10))
    plans = _schedule(ids, "ablation")
    protocol = P2ProtocolRecord(
        kind="p2_four_arm_ablation_protocol",
        generated_at=datetime.now(UTC),
        code_commit="fixture",
        offline_only=True,
        formal_ready=False,
        formal_missing=(),
        qualified_task_ids=ids,
        primary_task_ids=ids[:8],
        collection_failure_task_ids=ids[8:],
        task_hashes={},
        recipe_hashes={},
        budgets={},
        # Legacy labels must never determine the actual arms in a report.
        arm_configurations={"control": {}, "treatment": {}},
        schedule=plans,
    )
    (tmp_path / "protocol.json").write_text(protocol.model_dump_json(), encoding="utf-8")
    (tmp_path / "trials").mkdir()
    artifacts = {}
    for name, body in (
        ("run.json", json.dumps({"agent_config": {}, "context_metrics": {}})),
        (
            "verification.json",
            json.dumps(
                {
                    "eligible": True,
                    "evidence": {
                        "status": "passed",
                        "junit_available": True,
                        "audit_available": True,
                        "collection_audit_available": True,
                        "execution_audit_available": True,
                        "source_import_audit_valid": True,
                    },
                }
            ),
        ),
        ("patch.diff", "diff --git a/module.py b/module.py\n"),
    ):
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        artifacts[name] = (str(path), hashlib.sha256(path.read_bytes()).hexdigest())
    for plan in plans:
        record = P2TrialRecord(
            sequence=plan.sequence,
            task_id=plan.task_id,
            arm=plan.arm,
            repetition=plan.repetition,
            mode="simulation",
            status="verification_complete",
            independent_passed=True,
            input_tokens=100,
            output_tokens=10,
            agent_duration_seconds=2,
            verification_duration_seconds=1,
            calculated_cost_amount=0,
            run_result_path=artifacts["run.json"][0],
            run_result_sha256=artifacts["run.json"][1],
            verification_path=artifacts["verification.json"][0],
            verification_sha256=artifacts["verification.json"][1],
            agent_patch_path=artifacts["patch.diff"][0],
            agent_patch_sha256=artifacts["patch.diff"][1],
        )
        (tmp_path / "trials" / f"{plan.sequence:03d}.json").write_text(
            record.model_dump_json(), encoding="utf-8"
        )
    return tmp_path


def test_complete_four_arm_reports_and_entry_points(reporting_experiment):
    root = reporting_experiment
    summary = summarize_ablation(root)
    assert summary["planned_count"] == summary["valid_evidence_count"] == 120
    assert summary["mode"] == "simulation"
    assert summary["batch_complete"] is True
    assert summary["configuration_selection_ready"] is False
    assert summary == json.loads(write_p2_summary(root).read_text(encoding="utf-8"))
    with pytest.raises(BenchmarkError, match="require summarize_ablation"):
        summarize_p2_experiment(root)
    diagnostic = diagnose_p2_experiment(root)
    for task in summary["task_results"]:
        diagnosed = diagnostic["task_arm_summaries"][task["task_id"]]
        assert set(diagnosed) == set(task["arms"])
        for arm, values in task["arms"].items():
            assert values["success_rate"] == values["planned_success_lower_bound"] == 1
            assert diagnosed[arm]["trial_count"] == diagnosed[arm]["success_count"] == 3


def test_validation_closure_summary_applies_preregistered_success_gate(tmp_path):
    ids = tuple(f"ordinary-{index}" for index in range(8))
    plans = _schedule(ids, "validation_closure")
    qualifications = {
        task_id: P2QualificationRecord(
            task_id=task_id,
            qualification_type="ordinary_assertion_failure",
            base_commit="fixture",
            dependency_fingerprint="deps",
            expected_node_ids=(f"tests/{task_id}.py::test_case",),
        )
        for task_id in ids
    }
    protocol = P2ProtocolRecord(
        kind="p2_validation_closure_protocol",
        generated_at=datetime.now(UTC),
        code_commit="fixture",
        offline_only=True,
        formal_ready=False,
        formal_missing=(),
        qualified_task_ids=ids,
        primary_task_ids=ids,
        collection_failure_task_ids=(),
        task_hashes={},
        recipe_hashes={},
        p1_qualifications=qualifications,
        budgets={},
        arm_configurations={"no_compaction": {}, "validation_closure": {}},
        schedule=plans,
    )
    (tmp_path / "protocol.json").write_text(protocol.model_dump_json(), encoding="utf-8")
    trial_dir = tmp_path / "trials"
    trial_dir.mkdir()
    artifacts = {}
    for name, payload in (
        ("run.json", {"agent_config": {}, "context_metrics": {}}),
        (
            "verification.json",
            {
                "eligible": True,
                "evidence": {
                    "status": "passed",
                    "junit_available": True,
                    "audit_available": True,
                    "collection_audit_available": True,
                    "execution_audit_available": True,
                    "source_import_audit_valid": True,
                },
            },
        ),
    ):
        path = tmp_path / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        artifacts[name] = (str(path), hashlib.sha256(path.read_bytes()).hexdigest())
    patch_path = tmp_path / "patch.diff"
    patch_path.write_text("diff --git a/module.py b/module.py\n", encoding="utf-8")
    patch_identity = (str(patch_path), hashlib.sha256(patch_path.read_bytes()).hexdigest())
    failed_verification_path = tmp_path / "verification-failed.json"
    failed_verification_path.write_text(
        json.dumps(
            {
                "eligible": False,
                "reason": "tests_failed",
                "evidence": {
                    "status": "assertion_failed",
                    "junit_available": True,
                    "audit_available": True,
                    "collection_audit_available": True,
                    "execution_audit_available": True,
                    "source_import_audit_valid": True,
                },
            }
        ),
        encoding="utf-8",
    )
    failed_verification_identity = (
        str(failed_verification_path),
        hashlib.sha256(failed_verification_path.read_bytes()).hexdigest(),
    )
    for plan in plans:
        task_index = ids.index(plan.task_id)
        if plan.arm.value == "no_compaction":
            passed = task_index < 4
        else:
            passed = task_index < 5 or (task_index == 5 and plan.repetition == 1)
        verification_identity = (
            artifacts["verification.json"] if passed else failed_verification_identity
        )
        record = P2TrialRecord(
            sequence=plan.sequence,
            task_id=plan.task_id,
            arm=plan.arm,
            repetition=plan.repetition,
            mode="formal",
            status="verification_complete",
            stop_reason="agent_completed",
            independent_passed=passed,
            input_tokens=100,
            output_tokens=10,
            agent_duration_seconds=2,
            calculated_cost_amount=0,
            cost_currency="CNY",
            run_result_path=artifacts["run.json"][0],
            run_result_sha256=artifacts["run.json"][1],
            verification_path=verification_identity[0],
            verification_sha256=verification_identity[1],
            agent_patch_path=patch_identity[0],
            agent_patch_sha256=patch_identity[1],
        )
        (trial_dir / f"{plan.sequence:03d}.json").write_text(
            record.model_dump_json(), encoding="utf-8"
        )

    summary = summarize_p2_experiment(tmp_path)
    assert summary.planned_count == summary.completed_count == 48
    assert summary.evidence_valid_count == 48
    assert summary.infrastructure_error_count == 0
    assert summary.primary_control_success_count == 12
    assert summary.primary_treatment_success_count == 16
    assert summary.validation_closure_net_success_gain == 4
    assert summary.validation_closure_tasks_with_gain == 2
    assert summary.validation_closure_adoptable is True
    assert all(task.planned_count == 6 for task in summary.task_summaries)
    report_path = write_p2_summary(tmp_path)
    report = report_path.with_suffix(".md").read_text(encoding="utf-8")
    assert "符合门槛：True" in report
    assert "正式模型结果描述本批固定开发题上的观察" in report
    assert "模拟结果仅验证工程流程" not in report

    changed_plan = next(
        plan
        for plan in plans
        if plan.arm.value == "validation_closure"
        and plan.task_id == ids[5]
        and plan.repetition == 1
    )
    changed_path = trial_dir / f"{changed_plan.sequence:03d}.json"
    changed_record = P2TrialRecord.model_validate_json(changed_path.read_text(encoding="utf-8"))
    changed_record = changed_record.model_copy(
        update={
            "independent_passed": False,
            "verification_path": failed_verification_identity[0],
            "verification_sha256": failed_verification_identity[1],
        }
    )
    changed_path.write_text(changed_record.model_dump_json(), encoding="utf-8")
    below_threshold = summarize_p2_experiment(tmp_path)
    assert below_threshold.primary_treatment_success_count == 15
    assert below_threshold.validation_closure_adoptable is False


@pytest.mark.parametrize("status", ["report_missing", "execution_error"])
def test_verification_error_is_not_an_observed_repair_failure(reporting_experiment, status):
    root = reporting_experiment
    evidence = {
        "status": status,
        "error_count": 1,
        "audit_diagnostic": "audit is missing setup, call, or teardown evidence",
    }
    path = root / "verification.json"
    path.write_text(json.dumps({"eligible": False, "evidence": evidence}), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for trial_path in (root / "trials").glob("*.json"):
        record = json.loads(trial_path.read_text(encoding="utf-8"))
        record.update(
            verification_sha256=digest, independent_passed=False, stop_reason="agent_completed"
        )
        trial_path.write_text(json.dumps(record), encoding="utf-8")
    summary = summarize_ablation(root)
    assert summary["verification_error_count"] == 120
    assert summary["repair_failure_count"] == summary["infrastructure_count"] == 0
    assert summary["valid_evidence_count"] == 0
    for row in summary["trials"]:
        assert row["agent_termination_category"] == "agent_completed"
        assert row["termination_category"] == "verification_indeterminate"
        assert row["verification_status"] == status
        assert row["verification_error_count"] == 1
    assert summary["task_results"][0]["arms"]["no_compaction"]["success_rate"] is None


@pytest.mark.parametrize(
    "task_id,entry,module,symbol",
    [
        (
            "pylint-dev__pylint-4551",
            "tests/unittest_pyreverse_writer.py",
            "pylint.pyreverse.utils",
            "get_annotation",
        ),
        (
            "pylint-dev__pylint-4604",
            "tests/checkers/unittest_variables.py",
            "pylint.constants",
            "IS_PYPY",
        ),
    ],
)
def test_only_exact_reviewed_collection_failure_is_scoreable(task_id, entry, module, symbol):
    evidence = {
        "status": "collection_error",
        "collection_audit_available": True,
        "source_import_audit_valid": True,
        "collection": {"returncode": 2},
        "collection_error_records": [
            {
                "nodeid": entry,
                "longrepr": f"ImportError: cannot import name '{symbol}' from '{module}'",
            }
        ],
    }
    payload = {"eligible": False, "evidence": evidence}
    assert _verification_failure_kind(task_id, payload) is None
    evidence["source_import_audit_valid"] = False
    assert _verification_failure_kind(task_id, payload) == "unreviewed_collection_error"
    evidence["source_import_audit_valid"] = True
    assert _verification_failure_kind("ordinary-task", payload) == "unreviewed_collection_error"
    evidence["collection_error_records"][0]["nodeid"] = "different_test.py"
    assert _verification_failure_kind(task_id, payload) == "unreviewed_collection_error"
    evidence["collection_error_records"][0]["nodeid"] = entry
    evidence["collection_error_records"][0]["longrepr"] = "ImportError: unrelated failure"
    assert _verification_failure_kind(task_id, payload) == "unreviewed_collection_error"


@pytest.mark.parametrize("damage", ["missing", "tampered", "incomplete"])
def test_unknown_success_does_not_become_failure(reporting_experiment, damage):
    root = reporting_experiment
    trial_path = root / "trials" / "001.json"
    if damage == "missing":
        trial_path.unlink()
    else:
        record = json.loads(trial_path.read_text(encoding="utf-8"))
        if damage == "tampered":
            record["run_result_sha256"] = "0" * 64
        else:
            record["status"] = "agent_completed"
        trial_path.write_text(json.dumps(record), encoding="utf-8")
    summary = summarize_ablation(root)
    assert summary["planned_count"] == len(summary["trials"]) == 120
    baseline = summary["task_results"][0]["arms"]["no_compaction"]
    assert baseline["success_rate"] is None
    assert baseline["input_tokens"] is None
    assert baseline["planned_success_lower_bound"] == 2 / 3
    for comparison in summary["comparisons"]["ordinary"].values():
        assert comparison["task_equal_weight_delta"]["success_rate"] is None
    assert summary == json.loads(write_p2_summary(root).read_text(encoding="utf-8"))


def test_target_mismatch_is_indeterminate_but_assertion_failure_is_scoreable():
    payload = {
        "eligible": False,
        "evidence": {
            "status": "assertion_failed",
            "error_count": 0,
            "junit_available": True,
            "audit_available": True,
            "collection_audit_available": True,
            "execution_audit_available": True,
            "source_import_audit_valid": True,
        },
    }
    assert _verification_failure_kind("ordinary-task", payload) is None
    payload["reason"] = "executed_node_ids_differ_from_frozen_p1_set"
    payload["evidence"]["status"] = "passed"
    assert _verification_failure_kind("ordinary-task", payload) == "frozen_target_mismatch"


@pytest.mark.parametrize("eligible", [True, False])
def test_missing_pytest_evidence_is_not_a_verdict(eligible):
    assert (
        _verification_failure_kind("task", {"eligible": eligible})
        == "missing_verification_evidence"
    )
    for evidence in ({}, {"status": "unknown"}, {"status": "assertion_failed"}):
        assert (
            _verification_failure_kind(
                "task",
                {
                    "eligible": True,
                    "evidence": evidence,
                },
            )
            == "invalid_success_status"
        )
    assert (
        _verification_failure_kind(
            "task",
            {
                "eligible": False,
                "reason": "empty_agent_patch",
                "patch_applied": False,
            },
        )
        is None
    )


@pytest.mark.parametrize("status", ["passed", "assertion_failed"])
def test_incomplete_audit_cannot_be_scored(status):
    assert (
        _verification_failure_kind(
            "task",
            {
                "eligible": status == "passed",
                "evidence": {"status": status, "audit_available": False},
            },
        )
        == "incomplete_verification_audit"
    )


def _make_formal_reporting_fixture(root):
    ledger_path = root / "shared-campaign.json"
    ledger_path.write_text(json.dumps({"halt_reason": None, "uncertain_request": False}))
    protocol_path = root / "protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    protocol.update(
        offline_only=False,
        formal={
            "model_name": "fixture/model",
            "provider": "fixture",
            "pricing_source": "fixture",
            "total_cost_cap_usd": 100,
            "input_cost_per_million_usd": 1,
            "output_cost_per_million_usd": 1,
            "campaign_ledger_path": str(ledger_path),
        },
    )
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")
    for path in (root / "trials").glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        record["mode"] = "formal"
        path.write_text(json.dumps(record), encoding="utf-8")
    return ledger_path


def test_formal_complete_report_can_select_configuration(reporting_experiment):
    root = reporting_experiment
    _make_formal_reporting_fixture(root)
    summary = summarize_ablation(root)
    assert summary["mode"] == "formal"
    assert summary["batch_complete"] and summary["configuration_selection_ready"]
    write_p2_summary(root)
    assert "模拟演练仅" not in (root / "p2-summary.md").read_text(encoding="utf-8")
    assert "Simulation verifies" not in summary["interpretation"]


@pytest.mark.parametrize("stop_reason", ["campaign_budget_exhausted", "cost_cap_would_be_exceeded"])
def test_frozen_budget_stop_preserves_plan_without_false_corruption(
    reporting_experiment, stop_reason
):
    root = reporting_experiment
    _make_formal_reporting_fixture(root)
    for sequence in (1, *range(9, 121)):
        (root / "trials" / f"{sequence:03d}.json").unlink()
    (root / "summary.json").write_text(
        json.dumps(
            {
                "campaign_stop_reason": stop_reason,
                "next_sequence": 9,
            }
        ),
        encoding="utf-8",
    )
    summary = summarize_ablation(root)
    assert summary["planned_count"] == len(summary["trials"]) == 120
    assert summary["unexecuted_count"] == 113
    assert summary["evidence_issue_count"] == 1  # Earlier missing record remains corruption.
    assert summary["trials"][0]["unexecuted_reason"] == "trial_record_missing"
    assert summary["trials"][8]["unexecuted_reason"] == "campaign_budget_exhausted"
    assert not summary["batch_complete"] and not summary["configuration_selection_ready"]
    assert summary["task_results"][0]["arms"]["no_compaction"]["success_rate"] is None


def test_later_ledger_halt_does_not_rewrite_legacy_batch_stop(reporting_experiment):
    root = reporting_experiment
    ledger = _make_formal_reporting_fixture(root)
    (root / "trials" / "120.json").unlink()
    ledger.write_text(json.dumps({"halt_reason": "cost_cap_would_be_exceeded"}), encoding="utf-8")
    summary = summarize_ablation(root)
    assert summary["campaign_stop_reason"] is None
    assert summary["evidence_issue_count"] == 1
    assert summary["trials"][-1]["unexecuted_reason"] == "trial_record_missing"
