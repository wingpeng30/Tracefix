from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path

import pytest

import tracefix.detailed_ablation as detailed
from tracefix.cli import main
from tracefix.detailed_ablation import (
    _cycle_difference,
    _cycle_line,
    _finite_nonnegative,
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
    event = facts["model_cycles"][0]["context"]["history_compaction_events"][0]
    assert event["tool_pruning"] is True
    assert event["history_fold"] is False
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


def test_tool_facts_tracks_repository_context_and_tools_between_requests() -> None:
    """离线诊断应将索引、Repo Map 和响应后工具调用归入正确请求周期。"""
    raw = "src/core.py:12 AssertionError tests/test_mod.py::test_a"
    trace = [
        _event("repository_indexed", {"duration_ms": 1250}, "index-1"),
        _event("repo_map_added", {}, "map-1"),
        _event(
            "tool_called",
            {
                "call": {
                    "id": "call-1",
                    "name": "read_file",
                    "arguments": {"path": "D:/repo/src/core.py"},
                }
            },
            "called-1",
        ),
        _event(
            "tool_returned",
            {
                "result": {
                    "call_id": "call-1",
                    "tool_name": "read_file",
                    "success": True,
                    "duration_ms": 10,
                    "output": raw,
                }
            },
            "returned-1",
        ),
        _event(
            "tool_result_presented",
            {
                "call_id": "call-1",
                "tool_name": "read_file",
                "original_chars": 100,
                "presented_chars": len(raw),
                "compacted": True,
            },
            "presented-1",
        ),
        _event(
            "context_prepared",
            {
                "estimated_tokens_before": 1000,
                "estimated_tokens_after": 1000,
                "tool_results_pruned": 0,
                "messages_compacted": 0,
                "batches_compacted": 0,
                "compacted": False,
            },
            "context-1",
        ),
        _event(
            "model_request_view",
            {
                "sanitized": True,
                "messages": [
                    {"role": "system", "content": "map", "metadata": {"kind": "repository_map"}},
                    {"role": "tool", "tool_call_id": "call-1", "content": raw},
                ],
                "tools": [],
            },
            "view-1",
        ),
        _event("model_requested", {"message_count": 2}, "requested-1"),
        _event(
            "message_added",
            {
                "message": {
                    "role": "assistant",
                    "content": "inspect next",
                    "tool_calls": [
                        {
                            "id": "call-2",
                            "name": "read_file",
                            "arguments": {"path": "src/other file.py"},
                        }
                    ],
                }
            },
            "assistant-1",
        ),
        _event(
            "model_responded",
            {"duration_ms": 25, "usage": {"input_tokens": 20, "output_tokens": 3}},
            "responded-1",
        ),
        _event(
            "tool_called",
            {
                "call": {
                    "id": "call-2",
                    "name": "read_file",
                    "arguments": {"path": "D:/repo/src/other file.py"},
                }
            },
            "called-2",
        ),
        _event(
            "tool_returned",
            {
                "result": {
                    "call_id": "call-2",
                    "tool_name": "read_file",
                    "success": True,
                    "duration_ms": 5,
                    "output": "no additional markers",
                }
            },
            "returned-2",
        ),
        _event(
            "tool_result_presented",
            {
                "call_id": "call-2",
                "tool_name": "read_file",
                "original_chars": 22,
                "presented_chars": 22,
                "compacted": False,
            },
            "presented-2",
        ),
        _event(
            "model_request_view",
            {
                "sanitized": True,
                "messages": [
                    {"role": "tool", "tool_call_id": "call-2", "content": "no additional markers"}
                ],
                "tools": [],
            },
            "view-2",
        ),
        _event("model_requested", {"message_count": 3}, "requested-2"),
        _event(
            "message_added",
            {"message": {"role": "assistant", "content": "done", "tool_calls": []}},
            "assistant-2",
        ),
        _event(
            "model_responded",
            {"duration_ms": 75, "usage": {"input_tokens": 30, "output_tokens": 4}},
            "responded-2",
        ),
    ]

    facts = _tool_facts(trace, "D:/repo")

    assert facts["repository_index_seconds"] == 1.25
    assert facts["repo_index_event_count"] == 1
    assert facts["repo_map_added_event_count"] == 1
    assert facts["model_response_event_seconds"] == 0.1
    assert facts["model_response_event_count"] == 2
    assert facts["model_cycles"][0]["repo_map_in_request"] is True
    assert facts["model_cycles"][0]["tools"][0]["name"] == "read_file"
    assert facts["model_cycles"][0]["tool_results"][0]["success"] is True
    assert facts["per_tool_result"][0]["was_shortened"] is True
    assert facts["per_tool_result"][1]["presented_to_next_request"] is True


@pytest.mark.parametrize(
    ("tool_pruned", "messages", "batches", "folded", "pruning", "fold"),
    [
        (2, 0, 0, False, True, False),
        (0, 4, 1, True, False, True),
        (2, 4, 1, True, True, True),
    ],
)
def test_context_events_classify_tool_pruning_and_history_folding(
    tool_pruned, messages, batches, folded, pruning, fold
) -> None:
    trace = _minimal_trace()
    prepared = next(event for event in trace if event["event_type"] == "context_prepared")
    prepared["payload"].update(
        tool_results_pruned=tool_pruned,
        messages_compacted=messages,
        batches_compacted=batches,
        compacted=folded,
    )
    compacted = next(event for event in trace if event["event_type"] == "context_compacted")
    compacted["payload"].update(
        tool_results_pruned=tool_pruned,
        messages_compacted=messages,
        batches_compacted=batches,
    )
    event = _tool_facts(trace, "D:/repo")["model_cycles"][0]["context"][
        "history_compaction_events"
    ][0]
    assert event["tool_pruning"] is pruning
    assert event["history_fold"] is fold


@pytest.mark.parametrize(
    ("prepared_updates", "event_updates", "remove_event_key"),
    [
        ({}, {}, "tool_results_pruned"),
        ({"tool_results_pruned": 2}, {"tool_results_pruned": 1}, None),
        (
            {"messages_compacted": 1, "batches_compacted": 0, "compacted": False},
            {"messages_compacted": 1},
            None,
        ),
    ],
)
def test_context_event_rejects_missing_conflicting_and_invalid_fold_counters(
    prepared_updates, event_updates, remove_event_key
) -> None:
    trace = _minimal_trace()
    prepared = next(event for event in trace if event["event_type"] == "context_prepared")
    prepared["payload"].update(prepared_updates)
    compacted = next(event for event in trace if event["event_type"] == "context_compacted")
    compacted["payload"].update(event_updates)
    if remove_event_key:
        compacted["payload"].pop(remove_event_key)
    with pytest.raises(BenchmarkError, match="context (event|fold marker)"):
        _tool_facts(trace, "D:/repo")


def test_prepared_context_rejects_missing_counters_instead_of_counting_zero() -> None:
    trace = _minimal_trace()
    prepared = next(event for event in trace if event["event_type"] == "context_prepared")
    prepared["payload"].pop("messages_compacted")
    with pytest.raises(BenchmarkError, match="prepared context"):
        _tool_facts(trace, "D:/repo")


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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0.0),
        (2.5, 2.5),
        (True, None),
        (-1, None),
        (float("nan"), None),
        ("4", None),
    ],
)
def test_metric_numbers_reject_booleans_negative_and_nonfinite_values(value, expected) -> None:
    assert _finite_nonnegative(value) == expected


@pytest.mark.parametrize(
    ("cycle", "stage", "expected"),
    [
        ({"request_view_line": 4}, "request_view_sha256", 4),
        ({"context": {"line": 5}}, "context", 5),
        ({"line": 6}, "context", 6),
        ({"assistant_decision": {"line": 7}}, "assistant_decision", 7),
        ({"tools": [{"line": 8}]}, "tool_results", 8),
        ({"tools": [{"line": 9}]}, "presentations", 9),
        ({"line": 10}, "presentations", 10),
        ({"line": 11}, "other", 11),
    ],
)
def test_cycle_line_selects_observed_event_or_safe_fallback(cycle, stage, expected) -> None:
    assert _cycle_line(cycle, stage) == expected


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


def test_detailed_writer_renders_new_reports_from_synthetic_summary(tmp_path, monkeypatch) -> None:
    ledger = tmp_path / "ledger.json"
    ledger.write_text('{"fixture": true}', encoding="utf-8")
    ledger_sha = hashlib.sha256(ledger.read_bytes()).hexdigest()
    experiment = tmp_path / "experiment"
    experiment.mkdir()
    output = tmp_path / "new-report"
    arms = ("action_optimization_bundle", "context_management_only")
    metric_names = (
        "agent_seconds", "verification_seconds", "model_request_seconds",
        "tool_execution_seconds", "test_tool_seconds_nested_in_tool_execution",
        "context_preparation_seconds", "repository_index_seconds", "unassigned_agent_seconds",
    )
    metrics = {
        arm: {"task_equal_weight_mean": dict.fromkeys(metric_names, 0.0)} for arm in arms
    }
    mechanism_names = (
        "provider_request_count", "repo_map_requests", "repo_map_candidate_reads",
        "repeated_request_tool_views", "cached_tool_calls", "failed_tool_calls",
        "presentation_shortened_results", "presentation_lengthened_results",
        "context_tool_pruned_operations", "context_tool_pruning_events",
        "context_history_fold_events", "max_preparation_peak_before_estimate",
        "max_preparation_peak_after_estimate", "presentation_original_chars",
        "presentation_visible_chars",
    )
    mechanisms = {arm: dict.fromkeys(mechanism_names, 0) for arm in arms}
    pair = {
        "primary_ordinary": True,
        "direction": "regression",
        "task_id": "synthetic-fixture",
        "repetition": 1,
        "baseline_sequence": 1,
        "action_sequence": 2,
        "first_post_request_observable_difference": {
            "stage": "assistant_decision", "baseline_event_line": 1, "action_event_line": 2,
        },
        "baseline_failure": None,
        "action_failure": "fixture_failure",
        "input_delta_action_minus_baseline": 0,
        "agent_seconds_delta_action_minus_baseline": 0.0,
        "content_marker_count_changes": {},
    }
    summary = {
        "schema_version": 1,
        "protocol_sha256": "protocol-fixture",
        "input_artifact_sha256": "input-fixture",
        "execution_commit": "fixture-commit",
        "evidence": {"audited": 1, "valid": 1, "provider_responses_verified": 1},
        "paired_cases": [pair],
        "task_equal_weight_metrics": metrics,
        "mechanism_totals": mechanisms,
        "interpretation_limits": ["synthetic fixture only"],
    }
    monkeypatch.setattr(
        "tracefix.detailed_ablation._analyze", lambda *args: (summary, ledger_sha)
    )

    json_path, markdown_path, pairs_path = write_detailed_ablation_diagnostic(
        experiment,
        output_dir=output,
        ledger_path=ledger,
        campaign_before_path=ledger,
        trace_hash_lock_path=ledger,
    )

    assert json.loads(json_path.read_text(encoding="utf-8"))["execution_commit"] == "fixture-commit"
    assert "synthetic-fixture" in markdown_path.read_text(encoding="utf-8")
    assert json.loads(pairs_path.read_text(encoding="utf-8"))["ordinary_pair_count"] == 1
    assert (output / "evidence-index.json").is_file()


def test_detailed_analyzer_validates_a_complete_synthetic_frozen_batch(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "synthetic-formal-run"
    trials_dir = root / "trials"
    trials_dir.mkdir(parents=True)
    campaign = tmp_path / "campaign.json"
    before = tmp_path / "campaign-before.json"
    protocol_path = root / "protocol.json"
    lock_path = tmp_path / "trace-lock.json"
    ledger_identity = {
        "cap_amount": 100.0,
        "currency": "USD",
        "provider": "synthetic provider",
        "model_name": "deepseek/deepseek-flash",
        "protocol_identity": "protocol-fixture",
        "pricing_identity": "pricing-fixture",
    }
    before.write_text(json.dumps({**ledger_identity, "requests": []}), encoding="utf-8")
    task_ids = [f"fixture-task-{index}" for index in range(10)]
    primary, special = task_ids[:8], task_ids[8:]
    schedule = []
    for task_id in task_ids:
        for arm in detailed._ARMS:
            for repetition in range(1, 4):
                sequence = len(schedule) + 1
                schedule.append(
                    {
                        "sequence": sequence,
                        "task_id": task_id,
                        "arm": arm,
                        "repetition": repetition,
                    }
                )
    protocol = {
        "kind": "p2_four_arm_ablation_protocol",
        "mode": "formal",
        "code_commit": "synthetic-commit",
        "qualified_task_ids": task_ids,
        "primary_task_ids": primary,
        "collection_failure_task_ids": special,
        "schedule": schedule,
    }
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")
    protocol_sha = hashlib.sha256(protocol_path.read_bytes()).hexdigest()
    audit_rows = []
    trial_requests = []
    trace_hashes = {}
    ledger = {**ledger_identity, "requests": [], "uncertain_request": False,
              "halt_reason": None, "reserved_amount": 0}

    def write_json(path: Path, value: dict) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    for plan in schedule:
        sequence = plan["sequence"]
        attempt = f"fixture-attempt-{sequence:03d}"
        trace_path = root / "traces" / f"trace-{sequence:03d}.jsonl"
        trace_events = _minimal_trace()
        trace_events.insert(
            0,
            _event("task_started", {"task": "Synthetic bounded analysis fixture."},
                   f"task-start-{sequence}"),
        )
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(
            "\n".join(json.dumps(event, ensure_ascii=False) for event in trace_events),
            encoding="utf-8",
        )
        trace_sha = hashlib.sha256(trace_path.read_bytes()).hexdigest()
        relative_trace = detailed._relative(trace_path)
        trace_hashes[relative_trace] = trace_sha

        patch_path = root / "patches" / f"patch-{sequence:03d}.diff"
        patch_sha = hashlib.sha256(b"synthetic diff\n").hexdigest()
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_bytes(b"synthetic diff\n")
        verification_path = root / "verification" / f"verification-{sequence:03d}.json"
        verification_sha = write_json(verification_path, {"status": "synthetic"})
        run_path = root / "runs" / f"run-{sequence:03d}.json"
        write_json(
            run_path,
            {
                "trace_path": str(trace_path),
                "workspace": "fixture-workspace",
                "duration_seconds": 1.0,
                "model_request_seconds": 0.1,
                "tool_execution_seconds": 0.1,
                "context_preparation_seconds": 0.1,
                "repository_index_seconds": 0.1,
                "cached_tool_calls": 0,
                "repo_map": None,
                "presentation_metrics": {
                    "original_chars": 12,
                    "presented_chars": 12,
                    "compacted_result_count": 0,
                },
            },
        )
        response_path = root / "responses" / f"response-{sequence:03d}.json"
        request_id = f"{attempt}:1"
        response_usage = {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 10,
        }
        response_sha = write_json(
            response_path,
            {
                "request_id": request_id,
                "provider_model": "deepseek-flash",
                "provider_usage": response_usage,
            },
        )
        trial_requests.append(
            {
                "request_id": request_id,
                "status": "settled",
                "response_received": True,
                "calculated_cost_amount": 0.1,
                "response_evidence_path": str(response_path),
                "response_evidence_sha256": response_sha,
                "currency": "USD",
            }
        )
        write_json(
            trials_dir / f"{sequence:03d}.json",
            {
                **plan,
                "mode": "formal",
                "status": "verification_complete",
                "attempt_id": attempt,
                "stop_reason": "completed",
                "run_result_path": str(run_path),
                "agent_patch_path": str(patch_path),
                "agent_patch_sha256": patch_sha,
                "verification_path": str(verification_path),
                "verification_sha256": verification_sha,
                "input_tokens": 10,
                "output_tokens": 2,
                "calculated_cost_amount": 0.1,
                "agent_duration_seconds": 1.0,
                "verification_duration_seconds": 0.2,
            },
        )
        audit_rows.append(
            {
                "sequence": sequence,
                "evidence_valid": True,
                "independent_passed": sequence % 2 == 0,
                "verification_reason": None,
                "termination_category": "completed",
                "repo_map_candidate_reads": 0,
                "system_prompt_sha256": "prompt-fixture",
            }
        )
    ledger["requests"] = trial_requests
    campaign.write_text(json.dumps(ledger), encoding="utf-8")
    lock_path.write_text(
        json.dumps({
            "kind": "offline_context_stage_diagnostic",
            "execution_commit": "synthetic-commit",
            "trace_sha256": trace_hashes,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        detailed,
        "_audit_p2_evidence",
        lambda _root: {"protocol_sha256": protocol_sha, "rows": audit_rows},
    )

    report, unchanged_ledger_sha = detailed._analyze(root, campaign, before, lock_path)

    assert report["evidence"]["audited"] == 120
    assert report["evidence"]["provider_responses_verified"] == 120
    assert len(report["paired_cases"]) == 30
    assert report["paid_requests_made"] == report["provider_clients_constructed"] == 0
    assert unchanged_ledger_sha == hashlib.sha256(campaign.read_bytes()).hexdigest()
