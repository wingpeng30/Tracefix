"""Offline validation feedback diagnostics use only already saved artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.exceptions import BenchmarkError
from tracefix.validation_feedback import _analyze_trial, write_validation_feedback_diagnostic


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trial(root: Path, sequence: int, task_id: str, arm: str, repetition: int) -> dict:
    case = root / "runs" / f"{sequence:03d}"
    case.mkdir(parents=True)
    result = case / "result.json"
    patch = case / "patch.diff"
    verification = case / "verification.json"
    trace = case / "trajectory.jsonl"
    result.write_text(
        json.dumps({"agent_validation_status": "unverified", "stop_reason": "agent_completed"}),
        encoding="utf-8",
    )
    patch.write_text("", encoding="utf-8")
    verification.write_text("{}", encoding="utf-8")
    trace.write_text(
        json.dumps({"event_type": "run_provenance", "id": "provenance", "step": 0}) + "\n",
        encoding="utf-8",
    )
    trial = {
        "sequence": sequence,
        "task_id": task_id,
        "arm": arm,
        "repetition": repetition,
        "run_result_path": str(result),
        "agent_patch_path": str(patch),
        "verification_path": str(verification),
    }
    (root / "trials" / f"{sequence:03d}.json").write_text(
        json.dumps(trial), encoding="utf-8"
    )
    return {
        "sequence": sequence,
        "task_id": task_id,
        "arm": arm,
        "repetition": repetition,
        "evidence_valid": True,
        "artifact_hashes": {
            "run_result": _hash(result),
            "agent_patch": _hash(patch),
            "verification": _hash(verification),
        },
        "independent_passed": False,
        "verification_reason": "empty_agent_patch",
        "changed_paths": [],
    }


def test_trial_diagnostic_preserves_uncertain_test_feedback(tmp_path: Path) -> None:
    (tmp_path / "trials").mkdir()
    row = _trial(tmp_path, 1, "pytest-dev__pytest-10081", "validation_closure", 1)
    trace = tmp_path / "runs" / "001" / "trajectory.jsonl"
    events = [
        {"event_type": "run_provenance", "id": "provenance", "step": 0},
        {
            "event_type": "tool_called",
            "id": "patch-call",
            "step": 1,
            "payload": {"call": {"id": "p", "name": "apply_patch"}},
        },
        {
            "event_type": "tool_returned",
            "id": "patch-result",
            "step": 1,
            "payload": {
                "result": {
                    "tool_name": "apply_patch",
                    "success": True,
                    "output": {"changed_files": ["sample.py"]},
                }
            },
        },
        {
            "event_type": "tool_returned",
            "id": "test-result",
            "step": 2,
            "payload": {
                "result": {
                    "tool_name": "run_tests",
                    "success": False,
                    "output": {
                        "returncode": 0,
                        "test_status": "invalid_test_run",
                        "test_counts": {"passed": 1},
                        "diagnostic": "pytest audit lacks complete setup/call/teardown evidence",
                        "audit": {
                            "collected_node_ids": ["test.py::test_target", "test.py::test_other"],
                            "reports": [
                                {
                                    "nodeid": "test.py::test_target",
                                    "when": phase,
                                    "outcome": "passed",
                                }
                                for phase in ("setup", "call", "teardown")
                            ],
                        },
                    },
                }
            },
        },
    ]
    trace.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    record = _analyze_trial(tmp_path, row, focused=True)
    assert record["validation_category"] == "empty_final_patch"
    assert record["invalid_passing_test_candidates"] == 1
    assert record["selection_only_false_reject_candidates"] == 1
    assert record["tool_counts"]["patch_tool_successes"] == 1
    assert record["evidence_sha256"]["trace_current_sha256"] == _hash(trace)
    assert "pytest audit lacks complete" in json.dumps(record)

    result = tmp_path / "runs" / "001" / "result.json"
    result.write_text('{"agent_validation_status":"verified"}', encoding="utf-8")
    with pytest.raises(BenchmarkError, match="hash changed"):
        _analyze_trial(tmp_path, row, focused=True)


def test_full_offline_diagnostic_guards_provider_and_raw_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tracefix import validation_feedback
    from tracefix.models import litellm_adapter

    root = tmp_path / "experiment"
    (root / "trials").mkdir(parents=True)
    protocol = root / "protocol.json"
    protocol.write_text("{}", encoding="utf-8")
    ledger = root / "cost-ledger.json"
    ledger.write_text('{"requests": []}', encoding="utf-8")
    task_ids = (
        "pytest-dev__pytest-10081",
        "pylint-dev__pylint-4661",
        "pytest-dev__pytest-10356",
        "ordinary-4",
        "ordinary-5",
        "ordinary-6",
        "ordinary-7",
        "ordinary-8",
    )
    rows = []
    for task_id in task_ids:
        for repetition in range(1, 4):
            for arm in ("no_compaction", "validation_closure"):
                rows.append(_trial(root, len(rows) + 1, task_id, arm, repetition))
    fake_audit = {
        "protocol": SimpleNamespace(
            kind="p2_validation_closure_protocol", code_commit="fixed-commit", schedule=rows
        ),
        "protocol_sha256": _hash(protocol),
        "rows": rows,
    }
    monkeypatch.setattr(validation_feedback, "_audit_p2_evidence", lambda _: fake_audit)
    monkeypatch.setattr(
        litellm_adapter.LiteLLMAdapter,
        "__init__",
        lambda *args, **kwargs: pytest.fail("offline diagnostic constructed a provider client"),
    )
    ledger_before = _hash(ledger)
    output = tmp_path / "report"
    paths = write_validation_feedback_diagnostic(root, output_dir=output)
    assert len(paths) == 3
    report = json.loads(paths[0].read_text(encoding="utf-8"))
    assert report["positions"] == 48
    assert report["focus_case_count"] == 18
    assert len(report["focus_pairs"]) == 9
    assert report["agent_verified"] == 0
    assert _hash(ledger) == ledger_before
    assert "fixed-commit" in paths[1].read_text(encoding="utf-8")
    with pytest.raises(BenchmarkError, match="new output directory"):
        write_validation_feedback_diagnostic(root, output_dir=output)

    rows[0]["evidence_valid"] = False
    with pytest.raises(BenchmarkError, match="all 48"):
        write_validation_feedback_diagnostic(root, output_dir=tmp_path / "invalid")
