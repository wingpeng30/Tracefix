"""Offline validation feedback diagnostics use only already saved artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.exceptions import BenchmarkError
from tracefix.validation_feedback import (
    _analyze_trial,
    _events,
    _relative,
    _selector_tokens,
    _target_failures,
    write_validation_feedback_diagnostic,
)


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


def test_validation_feedback_trace_and_path_inputs_fail_closed(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-validation-feedback.jsonl"
    with pytest.raises(BenchmarkError, match="outside experiment"):
        _relative(tmp_path, outside)

    trace = tmp_path / "trace.jsonl"
    trace.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="line 1 is malformed"):
        _events(trace)
    trace.write_text('{"event_type": 7}\n', encoding="utf-8")
    with pytest.raises(BenchmarkError, match="line 1 is incomplete"):
        _events(trace)
    trace.write_text('{"event_type": "task_started"}\n', encoding="utf-8")
    with pytest.raises(BenchmarkError, match="lacks run provenance"):
        _events(trace)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (None, []),
        ("pytest tests/test_a.py", []),
        (["pytest", "--help", "tests/test_a.py::test_target", "C:/outside/test_b.py"],
         ["tests/test_a.py::test_target"]),
    ],
)
def test_selector_diagnostic_accepts_only_relative_python_selectors(command, expected) -> None:
    assert _selector_tokens(command) == expected


def test_target_failure_diagnostic_handles_optional_and_malformed_junit(tmp_path: Path) -> None:
    verification = tmp_path / "verification.json"
    verification.write_text("{}", encoding="utf-8")
    assert _target_failures(tmp_path, verification)["failed_tests"] is None

    audit = tmp_path / "audit.json"
    verification.write_text(
        json.dumps({"evidence": {"audit_path": str(audit)}}), encoding="utf-8"
    )
    assert _target_failures(tmp_path, verification)["junit_sha256"] is None

    junit = audit.with_name("junit.xml")
    junit.write_text("<testsuite><testcase>", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="JUnit is malformed"):
        _target_failures(tmp_path, verification)

    junit.write_text(
        '<testsuite><testcase name="failed"><failure/></testcase>'
        '<testcase name="passed"/></testsuite>',
        encoding="utf-8",
    )
    result = _target_failures(tmp_path, verification)
    assert result["failed_tests"] == ["failed"]
    assert result["junit_path"] == "junit.xml"


@pytest.mark.parametrize(
    ("events", "nonempty_patch", "status", "expected"),
    [
        ([], False, "unverified", "no_patch_attempt"),
        ([], True, "unverified", "no_agent_test"),
        ([{"event_type": "tool_returned", "step": 1, "payload": {"result": {
            "tool_name": "run_tests", "success": False,
            "output": {"test_status": "invalid_test_run", "returncode": 2},
        }}}], True, "unverified", "no_valid_test_pass"),
        ([{"event_type": "tool_returned", "step": 2, "payload": {"result": {
            "tool_name": "run_tests", "success": True,
            "output": {"test_status": "passed", "returncode": 0},
        }}}, {"event_type": "tool_returned", "step": 3, "payload": {"result": {
            "tool_name": "run_tests", "success": False,
            "output": {"test_status": "failed", "returncode": 1},
        }}}], True, "unverified", "later_test_failure"),
    ],
)
def test_trial_diagnostic_classifies_missing_or_invalid_validation(
    tmp_path: Path, events: list[dict], nonempty_patch: bool, status: str, expected: str
) -> None:
    (tmp_path / "trials").mkdir()
    row = _trial(tmp_path, 1, "ordinary-task", "no_compaction", 1)
    case = tmp_path / "runs" / "001"
    (case / "result.json").write_text(
        json.dumps({"agent_validation_status": status, "stop_reason": "completed"}),
        encoding="utf-8",
    )
    row["artifact_hashes"]["run_result"] = _hash(case / "result.json")
    if nonempty_patch:
        (case / "patch.diff").write_text("diff --git a/x.py b/x.py\n", encoding="utf-8")
        row["artifact_hashes"]["agent_patch"] = _hash(case / "patch.diff")
    trace = case / "trajectory.jsonl"
    if nonempty_patch:
        events = [
            {"event_type": "tool_called", "step": 1, "payload": {"call": {
                "id": "patch-call", "name": "apply_patch",
            }}},
            {"event_type": "tool_returned", "step": 1, "payload": {"result": {
                "tool_name": "apply_patch", "success": True,
                "output": {"changed_files": ["x.py"]},
            }}},
            *events,
        ]
    trace.write_text(
        json.dumps({"event_type": "run_provenance", "step": 0}) + "\n"
        + "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    record = _analyze_trial(tmp_path, row, focused=False)
    assert record["validation_category"] == expected
    assert record["resource_stop"] is False


def test_trial_diagnostic_detects_patch_after_last_pass(tmp_path: Path) -> None:
    (tmp_path / "trials").mkdir()
    row = _trial(tmp_path, 1, "ordinary-task", "no_compaction", 1)
    case = tmp_path / "runs" / "001"
    (case / "patch.diff").write_text("diff --git a/x.py b/x.py\n", encoding="utf-8")
    row["artifact_hashes"]["agent_patch"] = _hash(case / "patch.diff")
    events = [
        {"event_type": "tool_returned", "step": 2, "payload": {"result": {
            "tool_name": "run_tests", "success": True,
            "output": {"test_status": "passed", "returncode": 0},
        }}},
        {"event_type": "tool_returned", "step": 3, "payload": {"result": {
            "tool_name": "apply_patch", "success": True,
            "output": {"changed_files": ["x.py"]},
        }}},
    ]
    trace = case / "trajectory.jsonl"
    trace.write_text(
        json.dumps({"event_type": "run_provenance", "step": 0}) + "\n"
        + "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    record = _analyze_trial(tmp_path, row, focused=False)
    assert record["validation_category"] == "patch_after_last_pass"


@pytest.mark.parametrize(
    ("diff_step", "expected"),
    [(2, "missing_current_diff_confirmation"), (4, "unverified_needs_replay")],
)
def test_trial_diagnostic_requires_diff_to_follow_the_passing_test(
    tmp_path: Path, diff_step: int, expected: str
) -> None:
    (tmp_path / "trials").mkdir()
    row = _trial(tmp_path, 1, "ordinary-task", "no_compaction", 1)
    case = tmp_path / "runs" / "001"
    (case / "patch.diff").write_text("diff --git a/x.py b/x.py\n", encoding="utf-8")
    row["artifact_hashes"]["agent_patch"] = _hash(case / "patch.diff")
    events = [
        {"event_type": "tool_returned", "step": 1, "payload": {"result": {
            "tool_name": "apply_patch", "success": True,
            "output": {"changed_files": ["x.py"]},
        }}},
        {"event_type": "tool_returned", "step": 3, "payload": {"result": {
            "tool_name": "run_tests", "success": True,
            "output": {"test_status": "passed", "returncode": 0},
        }}},
        {"event_type": "tool_returned", "step": diff_step, "payload": {"result": {
            "tool_name": "get_git_diff", "success": True, "output": {"diff": "current"},
        }}},
    ]
    trace = case / "trajectory.jsonl"
    trace.write_text(
        json.dumps({"event_type": "run_provenance", "step": 0}) + "\n"
        + "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    record = _analyze_trial(tmp_path, row, focused=False)
    assert record["validation_category"] == expected
    assert record["tool_counts"]["get_git_diff_calls"] == 1


def test_trial_diagnostic_records_rejected_patch_and_invalid_test_candidate(tmp_path: Path) -> None:
    (tmp_path / "trials").mkdir()
    row = _trial(tmp_path, 1, "ordinary-task", "no_compaction", 1)
    case = tmp_path / "runs" / "001"
    (case / "patch.diff").write_text("diff --git a/x.py b/x.py\n", encoding="utf-8")
    row["artifact_hashes"]["agent_patch"] = _hash(case / "patch.diff")
    events = [
        {"event_type": "tool_returned", "step": 1, "payload": {"result": {
            "tool_name": "apply_patch", "success": False, "error": "patch does not change files",
            "metadata": {"error": {"code": "no_effect"}}, "output": {},
        }}},
        {"event_type": "tool_returned", "step": 2, "payload": {"result": {
            "tool_name": "run_tests", "success": False,
            "output": {"test_status": "invalid_test_run", "returncode": 0,
                       "test_counts": {"passed": 1, "skipped": 1}},
        }}},
    ]
    trace = case / "trajectory.jsonl"
    trace.write_text(
        json.dumps({"event_type": "run_provenance", "step": 0}) + "\n"
        + "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    record = _analyze_trial(tmp_path, row, focused=True)
    assert record["tool_counts"]["patch_tool_rejections"] == 1
    assert record["invalid_passing_test_candidates"] == 1
    assert record["selection_only_false_reject_candidates"] == 0
    assert record["timeline"][0]["error_kind"] == "no_effect"


def test_trial_diagnostic_accepts_current_diff_and_validation_trace(tmp_path: Path) -> None:
    (tmp_path / "trials").mkdir()
    row = _trial(tmp_path, 1, "ordinary-task", "no_compaction", 1)
    case = tmp_path / "runs" / "001"
    (case / "patch.diff").write_text("diff --git a/x.py b/x.py\n", encoding="utf-8")
    (case / "result.json").write_text(
        '{"agent_validation_status":"verified","stop_reason":"completed"}', encoding="utf-8"
    )
    row["artifact_hashes"]["agent_patch"] = _hash(case / "patch.diff")
    row["artifact_hashes"]["run_result"] = _hash(case / "result.json")
    events = [
        {"event_type": "tool_called", "step": 1, "payload": {"call": {
            "id": "patch-1", "name": "apply_patch",
        }}},
        {"event_type": "message_added", "step": 1, "id": "reminder", "payload": {
            "message": {"metadata": {"kind": "validation_required"}},
        }},
        {"event_type": "tool_returned", "step": 2, "payload": {"result": {
            "tool_name": "apply_patch", "success": True,
            "output": {"changed_files": ["x.py"]},
        }}},
        {"event_type": "tool_returned", "step": 3, "payload": {"result": {
            "tool_name": "run_tests", "success": True,
            "output": {"test_status": "passed", "returncode": 0,
                       "command": ["pytest", "tests/test_x.py"]},
        }}},
        {"event_type": "tool_returned", "step": 4, "payload": {"result": {
            "tool_name": "get_git_diff", "success": True, "output": {"diff": "diff"},
        }}},
    ]
    trace = case / "trajectory.jsonl"
    trace.write_text(
        json.dumps({"event_type": "run_provenance", "step": 0}) + "\n"
        + "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    record = _analyze_trial(tmp_path, row, focused=True)
    assert record["validation_category"] == "verified"
    assert record["verified_evidence_conflict"] is False
    assert record["tool_counts"]["validation_reminders"] == 1
    assert record["tool_counts"]["patch_tool_successes"] == 1
