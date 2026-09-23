from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path

import pytest

from tracefix.cli import main
from tracefix.detailed_ablation import (
    _cycle_difference,
    _normalize_tool_path,
    _task_equal_metrics,
    _tool_facts,
    _validate_trace,
    write_detailed_ablation_diagnostic,
)
from tracefix.exceptions import BenchmarkError


@pytest.fixture
def tmp_path():
    path = Path(__file__).parent / f".detailed-test-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path)


def _event(event_type: str, payload: dict, identity: str) -> dict:
    return {
        "id": identity,
        "event_type": event_type,
        "timestamp": "2026-09-23T00:00:00Z",
        "task_id": "fixture",
        "step": 1,
        "payload": payload,
    }


def _minimal_trace() -> list[dict]:
    return [
        _event(
            "tool_called",
            {"call": {"id": "call-1", "name": "read_file", "arguments": {"path": "D:/repo/a.py"}}},
            "called-1",
        ),
        _event(
            "tool_returned",
            {
                "result": {
                    "call_id": "call-1",
                    "tool_name": "read_file",
                    "success": True,
                    "duration_ms": 20,
                    "output": "assertionerror",
                }
            },
            "returned-1",
        ),
        _event(
            "tool_result_presented",
            {
                "call_id": "call-1",
                "tool_name": "read_file",
                "original_chars": 14,
                "presented_chars": 14,
                "compacted": False,
            },
            "presented-1",
        ),
        _event(
            "context_prepared",
            {
                "estimated_tokens_before": 33000,
                "estimated_tokens_after": 27000,
                "tool_results_pruned": 2,
                "messages_compacted": 0,
                "batches_compacted": 0,
                "compacted": False,
            },
            "context-1",
        ),
        _event(
            "context_compacted",
            {
                "estimated_tokens_saved": 6000,
                "tool_results_pruned": 2,
                "messages_compacted": 0,
                "batches_compacted": 0,
            },
            "context-fold-1",
        ),
        _event(
            "model_request_view",
            {
                "sanitized": True,
                "messages": [
                    {"role": "tool", "tool_call_id": "call-1", "content": "assertionerror"}
                ],
                "tools": [],
            },
            "view-1",
        ),
        _event("model_requested", {"message_count": 3}, "requested-1"),
        _event(
            "message_added",
            {"message": {"role": "assistant", "content": "done", "tool_calls": []}},
            "assistant-1",
        ),
        _event("model_responded", {"duration_ms": 12, "usage": {}}, "responded-1"),
    ]


def test_detailed_trace_validation_rejects_corrupt_unknown_and_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    valid = _minimal_trace()
    raw = "\n".join(json.dumps(item) for item in valid).encode()
    path.write_bytes(raw)
    events, digest = _validate_trace(path, hashlib.sha256(raw).hexdigest())
    assert len(events) == 9
    assert digest == hashlib.sha256(raw).hexdigest()

    path.write_bytes(raw + b"\nnot-json")
    with pytest.raises(BenchmarkError, match="malformed or unknown"):
        _validate_trace(path, hashlib.sha256(path.read_bytes()).hexdigest())

    duplicate = valid + [dict(valid[0])]
    duplicate_raw = "\n".join(json.dumps(item) for item in duplicate).encode()
    path.write_bytes(duplicate_raw)
    with pytest.raises(BenchmarkError, match="duplicate event"):
        _validate_trace(path, hashlib.sha256(duplicate_raw).hexdigest())

    with pytest.raises(BenchmarkError, match="hash"):
        _validate_trace(path, "0" * 64)


def test_tool_facts_checks_request_cycle_and_never_returns_raw_contents() -> None:
    facts = _tool_facts(_minimal_trace(), "D:/repo")
    result = facts["per_tool_result"][0]
    assert result["presented_to_next_request"] is True
    assert result["content_marker_retention"]["assertion_keyword"]["missing_unique"] == 0
    assert "assertionerror" not in json.dumps(facts["per_tool_result"]).casefold()
    assert "assertionerror" not in json.dumps(facts["model_cycles"]).casefold()
    assert "D:/repo" not in json.dumps(facts)
    assert facts["model_cycles"][0]["request_view_line"] == 6
    assert facts["model_cycles"][0]["repo_map_in_request"] is False
    assert (
        facts["model_cycles"][0]["context"]["history_compaction_events"][0][
            "estimated_tokens_saved"
        ]
        == 6000
    )

    with pytest.raises(BenchmarkError, match="request-view count"):
        _tool_facts(
            [event for event in _minimal_trace() if event["event_type"] != "model_request_view"],
            "D:/repo",
        )


def test_budget_blocked_request_tail_is_explicit_and_otherwise_rejected() -> None:
    trace = _minimal_trace()[:-2]
    with pytest.raises(BenchmarkError, match="ends before"):
        _tool_facts(trace, "D:/repo")
    facts = _tool_facts(trace, "D:/repo", allow_budget_blocked_tail=True)
    assert facts["budget_blocked_request_tail"] is True
    assert facts["model_cycles"][-1]["request_outcome"] == (
        "trial_budget_blocked_before_provider_response"
    )


def test_unexecuted_budget_skipped_test_call_is_explicitly_accounted() -> None:
    trace = _minimal_trace()
    assistant = next(event for event in trace if event["id"] == "assistant-1")
    assistant["payload"]["message"]["tool_calls"] = [
        {"id": "skipped-1", "name": "run_tests", "arguments": {"command": "pytest"}}
    ]
    trace.extend(
        [
            _event(
                "tool_returned",
                {
                    "result": {
                        "call_id": "skipped-1",
                        "tool_name": "run_tests",
                        "success": False,
                        "output": "",
                        "metadata": {"skipped": True, "reason": "test_limit_exceeded"},
                    }
                },
                "skipped-returned",
            ),
            _event(
                "tool_result_presented",
                {
                    "call_id": "skipped-1",
                    "tool_name": "run_tests",
                    "original_chars": 0,
                    "presented_chars": 0,
                    "compacted": False,
                },
                "skipped-presented",
            ),
        ]
    )
    facts = _tool_facts(trace, "D:/repo")
    assert facts["orphan_failed_test_results"] == 1
    assert facts["per_tool_result"][-1]["execution_status"] == "not_executed"
    assert facts["per_tool_result"][-1]["skip_reason"] == "test_limit_exceeded"


def test_workspace_paths_are_normalized_without_host_root() -> None:
    assert _normalize_tool_path("D:/work/task/src/a.py", "D:/work/task") == "repo/src/a.py"
    assert _normalize_tool_path("src/a.py", "D:/work/task") == "repo/src/a.py"
    assert _normalize_tool_path("C:/outside/a.py", "D:/work/task").startswith("external/")


def test_task_equal_metrics_averages_repetitions_before_task_weighting() -> None:
    rows = [
        {"task_id": "a", "arm": "base", "repetition": 1, "value": 1},
        {"task_id": "a", "arm": "base", "repetition": 2, "value": 2},
        {"task_id": "a", "arm": "base", "repetition": 3, "value": 3},
        {"task_id": "b", "arm": "base", "repetition": 1, "value": 9},
        {"task_id": "b", "arm": "base", "repetition": 2, "value": 9},
        {"task_id": "b", "arm": "base", "repetition": 3, "value": 9},
    ]
    summary = _task_equal_metrics(rows, ["a", "b"], ("base",), ("value",))
    assert summary["base"]["tasks"]["a"]["value"] == 2
    assert summary["base"]["task_equal_weight_mean"]["value"] == 5.5
    rows.pop()
    assert (
        _task_equal_metrics(rows, ["a", "b"], ("base",), ("value",))["base"]["tasks"]["b"]["value"]
        is None
    )


def test_cycle_difference_reports_first_difference_and_event_lines() -> None:
    base = [{"line": 4, "assistant_decision": {"line": 5, "hash": "a"}}]
    action = [{"line": 7, "assistant_decision": {"line": 8, "hash": "b"}}]
    found = _cycle_difference(base, action)
    assert found["stage"] == "assistant_decision"
    assert (found["baseline_event_line"], found["action_event_line"]) == (5, 8)
    assert _cycle_difference(base, base) is None


def test_detailed_cli_requires_explicit_offline_evidence_inputs(
    tmp_path: Path, monkeypatch
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("detailed mode must be wired to offline writer")

    monkeypatch.setattr("tracefix.cli.write_detailed_ablation_diagnostic", forbidden)
    with pytest.raises(SystemExit) as exc:
        main(["p2-diagnose", "--experiment-dir", str(tmp_path), "--detailed-ablation"])
    assert exc.value.code == 2


def test_default_diagnostic_cli_remains_compatible(tmp_path: Path, monkeypatch, capsys) -> None:
    expected = tmp_path / "p2-diagnostic.json"
    monkeypatch.setattr("tracefix.cli.write_p2_diagnostic", lambda root, output_dir=None: expected)
    assert main(["p2-diagnose", "--experiment-dir", str(tmp_path)]) == 0
    assert str(expected) in capsys.readouterr().out


def test_detailed_writer_never_overwrites_existing_output(tmp_path: Path, monkeypatch) -> None:
    ledger = tmp_path / "ledger.json"
    ledger.write_text("{}", encoding="utf-8")
    experiment = tmp_path / "experiment"
    experiment.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr(
        "tracefix.detailed_ablation._analyze",
        lambda *args: ({"protocol_sha256": "x", "paired_cases": []}, "0" * 64),
    )
    with pytest.raises(BenchmarkError, match="already exists"):
        write_detailed_ablation_diagnostic(
            experiment,
            output_dir=output,
            ledger_path=ledger,
            campaign_before_path=ledger,
            trace_hash_lock_path=ledger,
        )
