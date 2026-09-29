import hashlib
import json
from pathlib import Path

import pytest

from tracefix.holdout import freeze_holdout, freeze_long_context_mechanism


def test_freeze_holdout_uses_order_and_only_ordinary_qualification(tmp_path) -> None:
    def evidence(variant: str, status: str, returncode: int) -> dict:
        root = tmp_path / variant / ".tracefix-validation"
        root.mkdir(parents=True)
        node = "tests/test_x.py::test_x[p\\q]"
        source = root.parent / "pkg.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        for name in (
            "collection.stdout.txt",
            "collection.stderr.txt",
            "execution.stdout.txt",
            "execution.stderr.txt",
        ):
            (root / name).write_text("evidence", encoding="utf-8")
        for stage, exitstatus in (("collection", 0), ("execution", returncode)):
            reports = (
                []
                if stage == "collection"
                else [
                    {
                        "nodeid": node,
                        "when": phase,
                        "outcome": "failed" if phase == "call" and returncode else "passed",
                    }
                    for phase in ("setup", "call", "teardown")
                ]
            )
            (root / f"{stage}.audit.json").write_text(
                json.dumps(
                    {
                        "format_version": 2,
                        "run_id": f"run:{stage}",
                        "stage": stage,
                        "completed": True,
                        "exitstatus": exitstatus,
                        "collected_node_ids": [node],
                        "reports": reports,
                        "collection_errors": [],
                        "imported_source_paths": {"pkg": str(source)},
                    }
                ),
                encoding="utf-8",
            )
        failures = 1 if returncode else 0
        (root / "junit.xml").write_text(
            f'<testsuite tests="1" failures="{failures}" errors="0" skipped="0" />',
            encoding="utf-8",
        )
        return {
            "status": status,
            "returncode": returncode,
            "timed_out": False,
            "junit_available": True,
            "collection_audit_available": True,
            "execution_audit_available": True,
            "source_import_audit_valid": True,
            "skipped_count": 0,
            "xfailed_count": 0,
            "xpassed_count": 0,
            "error_count": 0,
            "test_count": 1,
            "failure_count": failures,
            "expected_node_ids": [node],
            "collected_node_ids": [node],
            "executed_node_ids": [node],
            "collection": {
                "returncode": 0,
                "working_directory": str(root.parent),
                "stdout_path": str(root / "collection.stdout.txt"),
                "stderr_path": str(root / "collection.stderr.txt"),
            },
            "execution": {
                "returncode": returncode,
                "working_directory": str(root.parent),
                "stdout_path": str(root / "execution.stdout.txt"),
                "stderr_path": str(root / "execution.stderr.txt"),
            },
            "audit_path": str(root / "execution.audit.json"),
        }

    order = ["one", "two", "blocked", "not-reproduced", "unvalidated", "broken", "three"]
    pool = {
        "requested_repositories": ["org/repo"],
        "selection_seed": 20260921,
        "candidate_order_sha256": hashlib.sha256("\n".join(order).encode()).hexdigest(),
        "candidate_order": order,
        "selected": [
            {"instance_id": "one", "repo": "org/repo", "base_commit": "abc"},
            {"instance_id": "two", "repo": "org/repo", "base_commit": "abc"},
            {"instance_id": "blocked", "repo": "org/repo", "base_commit": "abc"},
            {"instance_id": "not-reproduced", "repo": "org/repo", "base_commit": "abc"},
            {"instance_id": "unvalidated", "repo": "org/repo", "base_commit": "abc"},
            {"instance_id": "broken", "repo": "org/repo", "base_commit": "abc"},
            {"instance_id": "three", "repo": "org/repo", "base_commit": "abc"},
        ],
    }
    behavior = [
        {"task_id": "one", "eligible_for_llm_prescreen": False},
        {"task_id": "two", "reviewed_collection_failure": True},
        {"task_id": "blocked", "qualification_type": "environment_blocked"},
        {"task_id": "not-reproduced", "qualification_type": "business_not_reproduced"},
        {
            "task_id": "broken",
            "qualification_type": "assertion_failure",
            "eligible_for_llm_prescreen": False,
            "base_commit": "wrong",
            "dependency_drift_detected": True,
            "initial_evidence": {},
            "gold_evidence": {},
        },
        {
            "task_id": "three",
            "base_commit": "abc",
            "eligible_for_llm_prescreen": True,
            "qualification_type": "assertion_failure",
            "dependency_drift_detected": False,
            "initial_hidden_failed": True,
            "gold_hidden_passed": True,
            "test_environment": {"fingerprint_sha256": "f"},
            "environment_before": {"fingerprint_sha256": "f"},
            "environment_after_initial": {"fingerprint_sha256": "f"},
            "environment_after_gold": {"fingerprint_sha256": "f"},
            "initial_evidence": evidence("initial", "assertion_failed", 1),
            "gold_evidence": evidence("gold", "passed", 0),
        },
    ]
    pool_path = tmp_path / "pool.json"
    report_path = tmp_path / "behavior.json"
    pool_path.write_text(json.dumps(pool), encoding="utf-8")
    report_path.write_text(json.dumps(behavior), encoding="utf-8")
    result = freeze_holdout(pool_path, report_path, tmp_path / "freeze.json", per_repository=1)
    assert result["selected_task_ids"] == ["three"]
    assert [item["category"] for item in result["decisions"]] == [
        "not_qualified",
        "reviewed_collection_failure",
        "environment_blocked",
        "not_reproduced",
        "not_validated",
        "evidence_invalid",
        "ordinary_qualified",
    ]
    assert result["schema_version"] == 2
    accepted = result["decisions"][-1]
    assert accepted["base_commit"] == "abc"
    assert accepted["target_node_ids"] == [r"tests/test_x.py::test_x[p\q]"]
    assert accepted["dependency_fingerprint"] == "f"


def test_freeze_holdout_rejects_modified_order_and_duplicate_evidence(tmp_path) -> None:
    pool = {
        "requested_repositories": ["org/repo"],
        "candidate_order": ["one"],
        "candidate_order_sha256": "modified",
        "selected": [{"instance_id": "one", "repo": "org/repo"}],
    }
    pool_path = tmp_path / "pool.json"
    report_path = tmp_path / "behavior.json"
    pool_path.write_text(json.dumps(pool), encoding="utf-8")
    report_path.write_text(json.dumps([{"task_id": "one"}]), encoding="utf-8")
    import pytest

    with pytest.raises(ValueError, match="order hash mismatch"):
        freeze_holdout(pool_path, report_path, tmp_path / "freeze.json")

    pool["candidate_order_sha256"] = hashlib.sha256(b"one").hexdigest()
    pool_path.write_text(json.dumps(pool), encoding="utf-8")
    report_path.write_text(json.dumps([{"task_id": "one"}, {"task_id": "one"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate behavior records"):
        freeze_holdout(pool_path, report_path, tmp_path / "freeze.json")

    report_path.write_text(json.dumps({"task_id": "one"}), encoding="utf-8")
    with pytest.raises(ValueError, match="behavior report must be a list"):
        freeze_holdout(pool_path, report_path, tmp_path / "freeze.json")


def test_freeze_long_context_replays_two_independent_mechanism_tasks(tmp_path) -> None:
    result = freeze_long_context_mechanism(
        Path(__file__).parents[1] / "benchmarks" / "long_context_tasks",
        tmp_path / "work",
        tmp_path / "mechanism.json",
    )
    assert result["valid"] is True
    assert len(result["tasks"]) == 2
    assert all(item["contract_count"] == 24 for item in result["tasks"])
    assert all(item["control"]["compacted"] is False for item in result["tasks"])
    assert all(item["treatment"]["compacted"] is True for item in result["tasks"])
    assert all(item["critical_facts_retained"] is True for item in result["tasks"])
    assert all(item["critical_fact_checks"] for item in result["tasks"])


@pytest.mark.parametrize(
    "change,expected",
    [
        ("run_id", "audit_run_identity_mismatch"),
        ("outcome", "nonordinary_execution_outcome"),
        ("setup", "execution_fixture_failure"),
        ("failure_count", "execution_failure_count_mismatch"),
        ("source", "execution_source_identity_missing"),
        ("outside", "target_source_outside_checkout"),
        ("collection_error", "execution_collection_errors"),
        ("missing", "missing_artifact"),
        ("malformed", "malformed_execution_audit"),
        ("duplicate", "execution_phase_evidence_mismatch"),
    ],
)
def test_raw_audit_rejects_identity_stage_and_target_source_tampering(tmp_path, change, expected):
    from tracefix.holdout import _evidence_artifacts

    test_freeze_holdout_uses_order_and_only_ordinary_qualification(tmp_path)
    behavior = json.loads((tmp_path / "behavior.json").read_text(encoding="utf-8"))
    evidence = behavior[-1]["initial_evidence"]
    path = Path(evidence["audit_path"])
    audit = json.loads(path.read_text(encoding="utf-8"))
    if change == "run_id":
        audit["run_id"] = "another:execution"
    elif change == "outcome":
        audit["reports"][1]["outcome"] = "skipped"
    elif change == "setup":
        audit["reports"][0]["outcome"] = "failed"
    elif change == "failure_count":
        audit["reports"][1]["outcome"] = "passed"
    elif change == "source":
        audit["imported_source_paths"] = {"tests": str(path)}
    elif change == "outside":
        audit["imported_source_paths"] = {"pkg": str(tmp_path / "wrong" / "pkg.py")}
    elif change == "collection_error":
        audit["collection_errors"] = ["extra collection exception"]
    elif change == "duplicate":
        audit["reports"].append(audit["reports"][0])
    path.write_text(json.dumps(audit), encoding="utf-8")
    if change == "missing":
        path.unlink()
    elif change == "malformed":
        path.write_text("[", encoding="utf-8")
    _, errors = _evidence_artifacts(evidence, source_module="pkg")
    assert any(expected in error for error in errors)


@pytest.mark.parametrize("stage", ["gold", "collection"])
def test_freeze_entrypoint_rejects_tampered_gold_and_collection(tmp_path, stage):
    test_freeze_holdout_uses_order_and_only_ordinary_qualification(tmp_path)
    old_freeze = (tmp_path / "freeze.json").read_bytes()
    behavior = json.loads((tmp_path / "behavior.json").read_text(encoding="utf-8"))
    evidence = behavior[-1]["gold_evidence" if stage == "gold" else "initial_evidence"]
    path = Path(evidence["audit_path"])
    if stage == "collection":
        path = path.with_name("collection.audit.json")
    audit = json.loads(path.read_text(encoding="utf-8"))
    if stage == "gold":
        audit["reports"][1]["outcome"] = "skipped"
    else:
        audit["run_id"] = "different:collection"
    path.write_text(json.dumps(audit), encoding="utf-8")
    result = freeze_holdout(
        tmp_path / "pool.json",
        tmp_path / "behavior.json",
        tmp_path / "rejected.json",
        per_repository=1,
    )
    assert result["selected_task_ids"] == []
    assert result["decisions"][-1]["category"] == "evidence_invalid"
    assert (tmp_path / "freeze.json").read_bytes() == old_freeze
