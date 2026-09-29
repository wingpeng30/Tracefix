import hashlib
import json
from pathlib import Path

import pytest

from scripts.audit_docker_replays import audit
from scripts.docker_agent_e2e import (
    acceptance_signature,
    verify_image_identity,
    verify_run_identity,
)
from scripts.docker_reverify import portable, verify_input


def test_staged_input_hashes_fail_closed(tmp_path: Path) -> None:
    original = tmp_path / "patch.diff"
    original.write_bytes(b"patch")
    manifest = {"task_id": "sample-task", "files": {
        "patch.diff": hashlib.sha256(original.read_bytes()).hexdigest(),
    }}
    (tmp_path / "input-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert verify_input(tmp_path, "sample-task") == manifest
    original.write_bytes(b"changed")
    with pytest.raises(ValueError, match="staged input changed"):
        verify_input(tmp_path, "sample-task")
    with pytest.raises(ValueError, match="identity mismatch"):
        verify_input(tmp_path, "other-task")


def test_container_evidence_paths_become_relative(tmp_path: Path) -> None:
    result = portable({
        "audit_path": "/work/patch-1/task-agent/.tracefix-validation/execution.audit.json",
        "working_directory": "/work/patch-1/task-agent",
    }, tmp_path)
    assert result["audit_path"] == "evidence/patch-1/task-agent/execution.audit.json"
    assert result["working_directory"] == "container:/work/patch-1/task-agent"


def test_container_image_and_recovery_identity_fail_closed() -> None:
    verify_image_identity("sha256:fixed", "sha256:fixed")
    with pytest.raises(RuntimeError, match="image identity mismatch"):
        verify_image_identity("sha256:changed", "sha256:fixed")
    expected = {
        "task_id": "sample-task",
        "image_id": "sha256:fixed",
        "source_commit": "abc123",
        "input_manifest_sha256": "manifest-hash",
        "recipe_fingerprint": "recipe-hash",
    }
    verify_run_identity(expected, expected)
    changed = {**expected, "source_commit": "different-commit"}
    with pytest.raises(RuntimeError, match="saved run identity mismatch"):
        verify_run_identity(changed, expected)


def test_acceptance_signature_detects_repeat_disagreement() -> None:
    first = {
        "qualified": True,
        "result": {
            "eligible": True,
            "evidence": {
                "status": "passed",
                "returncode": 0,
                "test_count": 1,
                "expected_node_ids": ["tests/test_issue.py::test_case"],
                "executed_node_ids": ["tests/test_issue.py::test_case"],
            },
            "environment_before": {"fingerprint_sha256": "env"},
        },
    }
    second = json.loads(json.dumps(first))
    second["result"]["evidence"]["executed_node_ids"] = ["tests/test_issue.py::other"]
    assert acceptance_signature(first) != acceptance_signature(second)


def test_replay_audit_rejects_missing_evidence_and_repeat_disagreement(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence" / "audit.json"
    evidence.parent.mkdir()
    evidence.write_text("{}", encoding="utf-8")
    node = "tests/test_issue.py::test_case"

    def item(stage: str, repeat: int) -> dict:
        proof = {
            "audit_available": True, "junit_available": True,
            "collection_audit_available": True, "source_import_audit_valid": True,
            "skipped_count": 0, "xfailed_count": 0, "xpassed_count": 0,
            "executed_node_ids": [node],
        }
        result = (
            {"initial_evidence": proof, "gold_evidence": proof,
             "dependency_drift_detected": False}
            if stage == "qualification" else
            {"evidence": proof, "dependency_drift_detected": False}
        )
        return {"stage": stage, "repeat": repeat, "qualified": True,
                "result": result, "evidence_sha256": {
                    "evidence/audit.json": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                }}

    report = tmp_path / "report.json"
    payload = {"task_id": "sample", "sequence": 1, "results": [
        item("qualification", 1), item("qualification", 2),
        item("patch", 1), item("patch", 2),
    ]}
    report.write_text(json.dumps(payload), encoding="utf-8")
    assert audit(report)["repeat_consistent"] is True
    evidence.unlink()
    with pytest.raises(FileNotFoundError):
        audit(report)
    evidence.write_text("{}", encoding="utf-8")
    payload["results"][-1]["result"]["evidence"]["executed_node_ids"] = ["other"]
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="node sets disagree"):
        audit(report)
