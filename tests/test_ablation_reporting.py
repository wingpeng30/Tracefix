import hashlib
import json
from datetime import UTC, datetime

import pytest

from tracefix.ablation import summarize_ablation
from tracefix.exceptions import BenchmarkError
from tracefix.p2_protocol import (
    P2ProtocolRecord,
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
