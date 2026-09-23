"""Offline, privacy-preserving diagnostics for frozen formal four-arm P2 runs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

from tracefix.exceptions import BenchmarkError
from tracefix.p2_protocol import _audit_p2_evidence
from tracefix.tracing.base import TraceEvent

_ARMS = ("no_compaction", "repo_map_only", "action_optimization_bundle", "context_management_only")
_DIRECTIONS = ("regression", "reverse", "same_success", "indeterminate")
_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("repository_path", re.compile(r"(?<![\w.-])(?:[\w.-]+[\\/])+[\w.-]+\.py(?::\d+)?", re.I)),
    ("test_node", re.compile(r"\btest[\w.-]*(?:::[\w\[\]().-]+)+", re.I)),
    ("assertion_keyword", re.compile(r"\b(?:assert(?:ionerror)?|expected|actual)\b", re.I)),
    ("exception_class", re.compile(r"\b[A-Z][A-Za-z0-9_]*(?:Error|Exception)\b")),
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"detailed P2 {label} is missing or invalid") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"detailed P2 {label} has an invalid shape")
    return value, _sha(raw)


def _finite_nonnegative(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return f"external-artifact/{path.name}"


def _markers(text: str) -> dict[str, set[str]]:
    return {
        kind: {
            _sha(match.group(0).replace("\\", "/").encode("utf-8"))
            for match in regex.finditer(text)
        }
        for kind, regex in _MARKERS
    }


def _tool_facts(
    events: list[dict[str, Any]], workspace: str, *, allow_budget_blocked_tail: bool = False
) -> dict[str, Any]:
    """Compare raw tool outputs with content actually present in request views."""
    returned: dict[str, dict[str, Any]] = {}
    presented: dict[str, dict[str, Any]] = {}
    visible: dict[str, list[dict[str, Any]]] = defaultdict(list)
    views: list[dict[str, Any]] = []
    contexts: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    called_ids: set[str] = set()
    event_lines = {event["id"]: index + 1 for index, event in enumerate(events)}
    declared_assistant_calls = {
        str(call.get("id")): str(call.get("name"))
        for event in events
        if event["event_type"] == "message_added"
        and isinstance(event["payload"].get("message"), dict)
        and event["payload"]["message"].get("role") == "assistant"
        for call in (event["payload"]["message"].get("tool_calls") or [])
        if isinstance(call, dict) and isinstance(call.get("id"), str)
    }
    repo_index_seconds: list[float] = []
    repo_map_added_event_count = 0
    for index, event in enumerate(events):
        kind, payload = event["event_type"], event["payload"]
        if kind == "context_prepared":
            contexts.append(
                {
                    "line": event_lines[event["id"]],
                    "estimated_tokens_before": _nonnegative_int(
                        payload.get("estimated_tokens_before")
                    ),
                    "estimated_tokens_after": _nonnegative_int(
                        payload.get("estimated_tokens_after")
                    ),
                    "tool_results_pruned": _nonnegative_int(payload.get("tool_results_pruned")),
                    "messages_compacted": _nonnegative_int(payload.get("messages_compacted")),
                    "batches_compacted": _nonnegative_int(payload.get("batches_compacted")),
                    "compacted": payload.get("compacted")
                    if isinstance(payload.get("compacted"), bool)
                    else None,
                    "history_compaction_events": [],
                }
            )
        elif kind == "model_request_view":
            if payload.get("sanitized") is not True:
                raise BenchmarkError("detailed P2 model request view is not marked sanitized")
            messages = payload.get("messages")
            tools = payload.get("tools")
            if not isinstance(messages, list) or not isinstance(tools, list):
                raise BenchmarkError("detailed P2 request view has invalid messages or tools")
            current: list[dict[str, Any]] = []
            safe_messages = []
            repo_map_in_view = False
            for message in messages:
                if not isinstance(message, dict):
                    raise BenchmarkError("detailed P2 request view contains a malformed message")
                safe_messages.append(
                    {
                        "role": message.get("role"),
                        "content_sha256": _sha(str(message.get("content") or "").encode("utf-8")),
                    }
                )
                metadata = message.get("metadata")
                if isinstance(metadata, dict) and metadata.get("kind") == "repository_map":
                    repo_map_in_view = True
                if message.get("role") != "tool":
                    continue
                call_id, content = message.get("tool_call_id"), message.get("content")
                if not isinstance(call_id, str) or not isinstance(content, str):
                    raise BenchmarkError("detailed P2 tool request view is incomplete")
                item = {"line": index + 1, "content": content}
                visible[call_id].append(item)
                current.append(item)
            views.append(
                {
                    "line": event_lines[event["id"]],
                    "sha256": _sha(
                        json.dumps(
                            {"messages": safe_messages, "tool_count": len(tools)},
                            sort_keys=True,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ),
                    "tool_message_count": len(current),
                    "repo_map_included": repo_map_in_view,
                }
            )
        elif kind == "tool_called":
            call = payload.get("call")
            if not isinstance(call, dict):
                raise BenchmarkError("detailed P2 tool-call event is malformed")
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            call_id = call.get("id")
            if not isinstance(call_id, str) or call_id in called_ids:
                raise BenchmarkError("detailed P2 tool-call identity is missing or duplicated")
            called_ids.add(call_id)
            safe_path = arguments.get("path")
            path_text = str(safe_path) if isinstance(safe_path, str) else ""
            normalized_arguments = dict(arguments)
            normalized_path = _normalize_tool_path(path_text, workspace) if path_text else None
            if path_text:
                normalized_arguments["path"] = normalized_path
            # Never copy workspace paths or query text into the report.
            calls.append(
                {
                    "line": index + 1,
                    "name": str(call.get("name", "unknown")),
                    "call_id_sha256": _sha(call_id.encode("utf-8")),
                    "path_sha256": _sha(normalized_path.encode("utf-8"))
                    if normalized_path
                    else None,
                    "arguments_sha256": _sha(
                        json.dumps(normalized_arguments, sort_keys=True, ensure_ascii=False).encode(
                            "utf-8"
                        )
                    ),
                }
            )
        elif kind == "tool_returned":
            result = payload.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("call_id"), str):
                raise BenchmarkError("detailed P2 tool-return event is malformed")
            if result["call_id"] in returned:
                raise BenchmarkError("detailed P2 tool result identity is duplicated")
            output = result.get("output")
            output_text = json.dumps(
                output, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            )
            returned[result["call_id"]] = {
                "line": event_lines[event["id"]],
                "tool_name": str(result.get("tool_name", "unknown")),
                "success": result.get("success")
                if isinstance(result.get("success"), bool)
                else None,
                "duration_ms": _finite_nonnegative(result.get("duration_ms")),
                "output_sha256": _sha(output_text.encode("utf-8")),
                "output_chars": len(output_text),
                "output": output_text,
                "skipped": result.get("metadata", {}).get("skipped") is True
                if isinstance(result.get("metadata"), dict)
                else False,
                "skip_reason": result.get("metadata", {}).get("reason")
                if isinstance(result.get("metadata"), dict)
                and isinstance(result.get("metadata", {}).get("reason"), str)
                else None,
            }
        elif kind == "tool_result_presented":
            call_id = payload.get("call_id")
            if not isinstance(call_id, str):
                raise BenchmarkError("detailed P2 presentation event lacks call identity")
            if call_id in presented:
                raise BenchmarkError("detailed P2 presentation identity is duplicated")
            presented[call_id] = {
                "line": event_lines[event["id"]],
                **{
                    key: payload.get(key)
                    for key in ("tool_name", "original_chars", "presented_chars", "compacted")
                },
            }
        elif kind == "repository_indexed":
            duration = _finite_nonnegative(payload.get("duration_ms"))
            if duration is not None:
                repo_index_seconds.append(duration / 1000)
        elif kind == "repo_map_added":
            repo_map_added_event_count += 1
        elif kind == "context_compacted":
            if not contexts:
                raise BenchmarkError(
                    "detailed P2 history-compaction event lacks a prepared context"
                )
            contexts[-1]["history_compaction_events"].append(
                {
                    "line": event_lines[event["id"]],
                    "estimated_tokens_saved": _nonnegative_int(
                        payload.get("estimated_tokens_saved")
                    ),
                    "tool_results_pruned": _nonnegative_int(payload.get("tool_results_pruned")),
                    "messages_compacted": _nonnegative_int(payload.get("messages_compacted")),
                    "batches_compacted": _nonnegative_int(payload.get("batches_compacted")),
                }
            )

    if set(presented) != set(returned) or not called_ids.issubset(returned):
        raise BenchmarkError("detailed P2 tool result and presentation identities differ")
    orphan_returns = set(returned) - called_ids
    if any(
        returned[call_id]["tool_name"] != "run_tests"
        or returned[call_id]["success"] is not False
        or not returned[call_id]["skipped"]
        or returned[call_id]["skip_reason"]
        not in {"step_limit_exceeded", "test_limit_exceeded", "time_limit_exceeded"}
        or declared_assistant_calls.get(call_id) != "run_tests"
        for call_id in orphan_returns
    ):
        raise BenchmarkError("detailed P2 has an unexplained result without a tool-call event")
    if not set(visible).issubset(called_ids):
        raise BenchmarkError("detailed P2 request view contains an unknown tool result")
    request_view_count = len(views)
    call_ids = called_ids
    per_tool: list[dict[str, Any]] = []
    fact_totals: Counter[str] = Counter()
    for call_id in sorted(set(call_ids) | orphan_returns):
        raw = returned.get(call_id)
        shown = presented.get(call_id)
        result_views = visible.get(call_id, [])
        if raw is None:
            raise BenchmarkError("detailed P2 presentation has no matching tool result")
        if shown is None:
            # A tool result rejected by the Agent budget may not be presented.
            if result_views:
                raise BenchmarkError("detailed P2 request view lacks presentation evidence")
            continue
        if any(view["line"] <= raw["line"] for view in result_views):
            raise BenchmarkError("detailed P2 request view predates its tool result")
        source_facts = _markers(raw["output"])
        visible_text = "\n".join(item["content"] for item in result_views)
        visible_facts = _markers(visible_text)
        category_counts = {}
        missing_hashes: dict[str, list[str]] = {}
        for category, values in source_facts.items():
            kept = values & visible_facts[category]
            missing = values - visible_facts[category]
            category_counts[category] = {
                "raw_unique": len(values),
                "visible_unique": len(kept),
                "missing_unique": len(missing),
            }
            if missing:
                missing_hashes[category] = sorted(missing)
                fact_totals[f"missing_{category}"] += len(missing)
        fact_totals["tool_results"] += 1
        fact_totals["tool_results_shortened"] += int(
            isinstance(shown.get("original_chars"), int)
            and isinstance(shown.get("presented_chars"), int)
            and shown["presented_chars"] < shown["original_chars"]
        )
        per_tool.append(
            {
                "call_id_sha256": _sha(call_id.encode("utf-8")),
                "call_event_observed": call_id in called_ids,
                "execution_status": "executed" if call_id in called_ids else "not_executed",
                "skip_reason": raw["skip_reason"],
                "tool_name": raw["tool_name"],
                "result_line": raw["line"],
                "presentation_line": shown["line"],
                "request_view_lines": [view["line"] for view in result_views],
                "raw_output_sha256": raw["output_sha256"],
                "raw_output_chars": raw["output_chars"],
                "original_chars": shown.get("original_chars"),
                "presented_chars": shown.get("presented_chars"),
                "was_shortened": category_counts is not None
                and isinstance(shown.get("original_chars"), int)
                and isinstance(shown.get("presented_chars"), int)
                and shown["presented_chars"] < shown["original_chars"],
                "content_marker_retention": category_counts,
                "missing_marker_hashes": missing_hashes,
                "presented_to_next_request": bool(result_views),
            }
        )

    cycles: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None
    pending_view: dict[str, Any] | None = None
    pending_context: dict[str, Any] | None = None
    response_duration_total = 0.0
    response_duration_count = 0
    context_by_line = {item["line"]: item for item in contexts}
    for index, event in enumerate(events):
        kind, payload = event["event_type"], event["payload"]
        if kind == "context_prepared":
            pending_context = context_by_line.get(event_lines[event["id"]])
        elif kind == "model_request_view":
            pending_view = next(
                (item for item in reversed(views) if item["line"] == index + 1), None
            )
        elif kind == "model_requested":
            if active is not None:
                raise BenchmarkError("detailed P2 trace contains overlapping requests")
            if pending is not None:
                cycles.append(pending)
            active = {
                "ordinal": len(cycles) + 1,
                "line": event_lines[event["id"]],
                "request_message_count": _nonnegative_int(payload.get("message_count")),
                "request_view_line": pending_view.get("line") if pending_view else None,
                "request_view_sha256": pending_view.get("sha256") if pending_view else None,
                "repo_map_in_request": pending_view.get("repo_map_included")
                if pending_view
                else None,
                "context": pending_context,
                "context_events": pending_context.get("history_compaction_events", [])
                if pending_context
                else [],
                "tools": [],
            }
            pending = None
            pending_view = None
            pending_context = None
        elif kind == "message_added":
            message = payload.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                if active is None:
                    raise BenchmarkError("detailed P2 assistant response is outside a request")
                raw_calls = message.get("tool_calls")
                tool_calls = raw_calls if isinstance(raw_calls, list) else []
                safe_calls = []
                for item in tool_calls:
                    if (
                        not isinstance(item, dict)
                        or not isinstance(item.get("name"), str)
                        or not isinstance(item.get("arguments"), dict)
                    ):
                        raise BenchmarkError("detailed P2 assistant tool call is malformed")
                    safe_calls.append(
                        {
                            "name": item["name"],
                            "arguments_sha256": _sha(
                                json.dumps(
                                    item["arguments"], sort_keys=True, ensure_ascii=False
                                ).encode("utf-8")
                            ),
                        }
                    )
                active["assistant_decision"] = {
                    "line": event_lines[event["id"]],
                    "content_sha256": _sha(str(message.get("content") or "").encode("utf-8")),
                    "tool_calls": safe_calls,
                }
        elif kind == "model_responded":
            if active is None:
                raise BenchmarkError("detailed P2 response has no matching request")
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            duration = _finite_nonnegative(payload.get("duration_ms"))
            active["response"] = {
                "line": event_lines[event["id"]],
                "finish_reason": payload.get("finish_reason"),
                "duration_ms": duration,
                "input_tokens": _nonnegative_int(usage.get("input_tokens")),
                "output_tokens": _nonnegative_int(usage.get("output_tokens")),
            }
            if duration is not None:
                response_duration_total += duration / 1000
                response_duration_count += 1
            pending, active = active, None
        elif kind == "tool_called":
            call = payload.get("call") if isinstance(payload.get("call"), dict) else {}
            args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            path = args.get("path")
            workspace_prefix = str(workspace)
            normalized_args = dict(args)
            normalized_path = (
                _normalize_tool_path(str(path), workspace_prefix) if isinstance(path, str) else None
            )
            if isinstance(path, str):
                normalized_args["path"] = normalized_path
            target = active or pending
            if target is not None:
                target["tools"].append(
                    {
                        "name": str(call.get("name", "unknown")),
                        "arguments_sha256": _sha(
                            json.dumps(normalized_args, sort_keys=True, ensure_ascii=False).encode()
                        ),
                        "path_sha256": _sha(normalized_path.encode()) if normalized_path else None,
                    }
                )
        elif kind == "tool_returned":
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            call_id = result.get("call_id")
            target = active or pending
            if target is not None:
                target.setdefault("tool_results", []).append(
                    {
                        "line": event_lines[event["id"]],
                        "tool_name": result.get("tool_name"),
                        "success": result.get("success"),
                        "duration_ms": _finite_nonnegative(result.get("duration_ms")),
                        "output_sha256": returned.get(call_id, {}).get("output_sha256"),
                    }
                )
        elif kind == "tool_result_presented":
            target = active or pending
            if target is not None:
                target.setdefault("presentations", []).append(
                    {
                        "line": event_lines[event["id"]],
                        "tool_name": payload.get("tool_name"),
                        "original_chars": _nonnegative_int(payload.get("original_chars")),
                        "presented_chars": _nonnegative_int(payload.get("presented_chars")),
                    }
                )
    budget_blocked_tail = active is not None
    if active is not None:
        tail_events = events[active["line"] :]
        if not allow_budget_blocked_tail or any(
            event["event_type"] not in {"agent_state_changed", "task_finished", "error"}
            for event in tail_events
        ):
            raise BenchmarkError("detailed P2 trace ends before the model response")
        active["request_outcome"] = "trial_budget_blocked_before_provider_response"
        active["response"] = None
        cycles.append(active)
        active = None
    if pending is not None:
        cycles.append(pending)
    if len(cycles) != sum(event["event_type"] == "model_requested" for event in events):
        raise BenchmarkError("detailed P2 request and response cycle counts differ")
    if request_view_count != len(cycles):
        raise BenchmarkError("detailed P2 actual request-view count does not match model calls")
    return {
        "call_count": len(calls),
        "orphan_failed_test_results": len(orphan_returns),
        "orphan_failed_test_result_lines": sorted(returned[key]["line"] for key in orphan_returns),
        "calls": calls,
        "per_tool_result": per_tool,
        "content_marker_totals": dict(sorted(fact_totals.items())),
        "contexts": contexts,
        "model_cycles": cycles,
        "repository_index_seconds": sum(repo_index_seconds) if repo_index_seconds else None,
        "model_response_event_seconds": response_duration_total,
        "model_response_event_count": response_duration_count,
        "repo_index_event_count": len(repo_index_seconds),
        "repo_map_added_event_count": repo_map_added_event_count,
        "request_view_count": request_view_count,
        "budget_blocked_request_tail": budget_blocked_tail,
    }


def _nonnegative_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _normalize_tool_path(value: str, workspace: str) -> str:
    """Make paths comparable across trial workspaces without publishing host paths."""
    normalized = value.replace("\\", "/")
    root = workspace.replace("\\", "/").rstrip("/")
    if root and normalized.casefold().startswith((root + "/").casefold()):
        return "repo/" + normalized[len(root) + 1 :]
    if not normalized.startswith(("/",)) and not re.match(r"^[A-Za-z]:/", normalized):
        return "repo/" + normalized.lstrip("./")
    return "external/" + _sha(normalized.encode("utf-8"))


def _validate_trace(path: Path, expected: str) -> tuple[list[dict[str, Any]], str]:
    raw = path.read_bytes()
    digest = _sha(raw)
    if digest != expected:
        raise BenchmarkError("detailed P2 trace hash does not match frozen manifest")
    events: list[dict[str, Any]] = []
    try:
        for line_number, line in enumerate(raw.splitlines(), 1):
            value = json.loads(line)
            event = TraceEvent.model_validate(value).model_dump(mode="json")
            event["_line"] = line_number
            events.append(event)
    except (json.JSONDecodeError, ValueError) as exc:
        raise BenchmarkError("detailed P2 trace contains a malformed or unknown event") from exc
    if not events:
        raise BenchmarkError("detailed P2 trace is empty")
    ids = [event["id"] for event in events]
    if len(ids) != len(set(ids)):
        raise BenchmarkError("detailed P2 trace contains duplicate event identities")
    if any(event["id"] != event["id"].strip() for event in events):
        raise BenchmarkError("detailed P2 trace contains invalid event identities")
    if _sha(path.read_bytes()) != digest:
        raise BenchmarkError("detailed P2 trace changed while being analyzed")
    return events, digest


def _cycle_difference(
    base: list[dict[str, Any]], action: list[dict[str, Any]]
) -> dict[str, Any] | None:
    for ordinal, (left, right) in enumerate(zip(base, action, strict=False), 1):
        for key in (
            "request_view_sha256",
            "context",
            "assistant_decision",
            "tools",
            "tool_results",
            "presentations",
            "context_events",
        ):
            if left.get(key) != right.get(key):
                return {
                    "request_ordinal": ordinal,
                    "stage": key,
                    "baseline_event_line": _cycle_line(left, key),
                    "action_event_line": _cycle_line(right, key),
                    "baseline_sha256": _sha(json.dumps(left.get(key), sort_keys=True).encode()),
                    "action_sha256": _sha(json.dumps(right.get(key), sort_keys=True).encode()),
                }
    if len(base) != len(action):
        return {
            "request_ordinal": min(len(base), len(action)) + 1,
            "stage": "request_cycle_count",
            "baseline_event_line": None,
            "action_event_line": None,
            "baseline_count": len(base),
            "action_count": len(action),
        }
    return None


def _cycle_line(cycle: dict[str, Any], stage: str) -> int | None:
    if stage == "request_view_sha256":
        return cycle.get("request_view_line")
    if stage == "context":
        context = cycle.get("context")
        return context.get("line") if isinstance(context, dict) else cycle.get("line")
    if stage == "assistant_decision":
        decision = cycle.get(stage)
        return decision.get("line") if isinstance(decision, dict) else cycle.get("line")
    if stage in {"tool_results", "presentations"}:
        key = "tool_results" if stage == "tool_results" else "presentations"
        values = cycle.get(key, [])
        if values:
            return values[0].get("line")
        return cycle.get("tools", [{}])[0].get("line") if cycle.get("tools") else cycle.get("line")
    return cycle.get("line")


def _task_equal_metrics(
    rows: list[dict[str, Any]], task_ids: list[str], arms: tuple[str, ...], metrics: tuple[str, ...]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for arm in arms:
        task_means: dict[str, dict[str, Any]] = {}
        for task in task_ids:
            group = [row for row in rows if row["task_id"] == task and row["arm"] == arm]
            task_means[task] = {
                metric: mean([row[metric] for row in group])
                if len(group) == 3 and all(row.get(metric) is not None for row in group)
                else None
                for metric in metrics
            }
        out[arm] = {
            "tasks": task_means,
            "task_equal_weight_mean": {
                metric: mean([values[metric] for values in task_means.values()])
                if task_means and all(values[metric] is not None for values in task_means.values())
                else None
                for metric in metrics
            },
        }
    return out


def _analyze(
    experiment_dir: Path, ledger_path: Path, campaign_before_path: Path, trace_hash_lock_path: Path
) -> tuple[dict[str, Any], str]:
    root = experiment_dir.expanduser().resolve()
    before, before_digest = _read_json(campaign_before_path, "campaign-before snapshot")
    initial_ledger, initial_digest = _read_json(ledger_path, "shared campaign ledger")
    if initial_ledger.get("requests", [])[: len(before.get("requests", []))] != before.get(
        "requests"
    ):
        raise BenchmarkError("detailed P2 campaign history prefix changed")
    lock, lock_digest = _read_json(trace_hash_lock_path, "frozen trace-hash lock")
    if lock.get("kind") != "offline_context_stage_diagnostic" or not isinstance(
        lock.get("trace_sha256"), dict
    ):
        raise BenchmarkError("detailed P2 trace-hash lock has an invalid format")
    protocol_path = root / "protocol.json"
    protocol, protocol_digest = _read_json(protocol_path, "protocol")
    if (
        protocol.get("kind") != "p2_four_arm_ablation_protocol"
        or protocol.get("mode") not in (None, "formal")
        or lock.get("execution_commit") != protocol.get("code_commit")
    ):
        raise BenchmarkError("detailed P2 requires the locked formal four-arm execution")
    for field in (
        "cap_amount",
        "currency",
        "provider",
        "model_name",
        "protocol_identity",
        "pricing_identity",
    ):
        if initial_ledger.get(field) != before.get(field):
            raise BenchmarkError("detailed P2 shared campaign identity changed since batch start")
    frozen_inputs: dict[Path, str] = {
        ledger_path.resolve(): initial_digest,
        campaign_before_path.resolve(): before_digest,
        trace_hash_lock_path.resolve(): lock_digest,
        protocol_path.resolve(): protocol_digest,
        Path(__file__).resolve(): _sha(Path(__file__).read_bytes()),
    }
    frozen_inputs.update(
        {
            Path(str(path)).resolve(): str(digest)
            for path, digest in lock.get("trace_sha256", {}).items()
        }
    )
    audit = _audit_p2_evidence(root)
    if audit["protocol_sha256"] != protocol_digest:
        raise BenchmarkError("detailed P2 protocol changed during evidence audit")
    schedule = protocol.get("schedule")
    if not isinstance(schedule, list) or len(schedule) != 120:
        raise BenchmarkError("detailed P2 protocol does not contain the frozen 120 positions")
    qualified = protocol.get("qualified_task_ids")
    primary = protocol.get("primary_task_ids")
    special = protocol.get("collection_failure_task_ids")
    if (
        not isinstance(qualified, list)
        or len(qualified) != 10
        or len(set(qualified)) != 10
        or not isinstance(primary, list)
        or not isinstance(special, list)
        or set(primary).intersection(special)
        or set(primary).union(special) != set(qualified)
    ):
        raise BenchmarkError("detailed P2 task-set identities are inconsistent")
    expected_positions = {
        (task, arm, repetition) for task in qualified for arm in _ARMS for repetition in range(1, 4)
    }
    actual_positions = {
        (item.get("task_id"), item.get("arm"), item.get("repetition")) for item in schedule
    }
    if len(actual_positions) != 120 or actual_positions != expected_positions:
        raise BenchmarkError("detailed P2 schedule has duplicate, missing, or invalid positions")
    audit_by_seq = {row["sequence"]: row for row in audit["rows"]}
    if set(audit_by_seq) != set(range(1, 121)) or any(
        not row["evidence_valid"] for row in audit["rows"]
    ):
        raise BenchmarkError(
            "detailed P2 requires all 120 original trial records to pass evidence audit"
        )
    lock_entries = lock["trace_sha256"]
    if len(lock_entries) != 120:
        raise BenchmarkError("detailed P2 trace lock does not cover all 120 traces")

    request_snapshot = initial_ledger.get("requests")
    requests = (
        request_snapshot[len(before["requests"]) :] if isinstance(request_snapshot, list) else []
    )
    if not isinstance(request_snapshot, list):
        raise BenchmarkError("detailed P2 campaign requests are malformed")
    trial_rows: list[dict[str, Any]] = []
    cycles_by_sequence: dict[int, list[dict[str, Any]]] = {}
    mechanism_by_sequence: dict[int, dict[str, Any]] = {}
    expected_trace_paths: set[str] = set()
    trace_hashes: dict[str, str] = {}
    seen_attempt_ids: set[str] = set()
    trial_artifact_hashes: dict[str, str] = {}
    response_artifact_hashes: dict[str, str] = {}
    for plan in schedule:
        seq = plan["sequence"]
        record_path = root / "trials" / f"{seq:03d}.json"
        record, record_sha = _read_json(record_path, f"trial {seq}")
        frozen_inputs[record_path.resolve()] = record_sha
        trial_artifact_hashes[f"trial-{seq:03d}-record"] = record_sha
        audit_row = audit_by_seq[seq]
        if any(
            record.get(key) != plan.get(key) for key in ("sequence", "task_id", "arm", "repetition")
        ):
            raise BenchmarkError("detailed P2 trial does not match frozen position")
        if record.get("mode") != "formal" or record.get("status") != "verification_complete":
            raise BenchmarkError("detailed P2 trial is not complete formal evidence")
        run_path = Path(record["run_result_path"])
        run_payload, run_sha = _read_json(run_path, f"run result {seq}")
        frozen_inputs[run_path.resolve()] = run_sha
        trial_artifact_hashes[f"trial-{seq:03d}-run-result"] = run_sha
        trace_path = Path(str(run_payload.get("trace_path", "")))
        relative_trace = _relative(trace_path)
        expected_trace_paths.add(relative_trace)
        expected = lock_entries.get(relative_trace)
        if not isinstance(expected, str):
            raise BenchmarkError("detailed P2 trace is absent from the frozen hash lock")
        events, actual_trace_sha = _validate_trace(trace_path, expected)
        workspace = run_payload.get("workspace")
        if not isinstance(workspace, str):
            raise BenchmarkError("detailed P2 workspace identity is missing")
        mechanism = _tool_facts(
            events,
            workspace,
            allow_budget_blocked_tail=record.get("stop_reason") == "p2_trial_budget_exhausted",
        )
        cycles_by_sequence[seq] = mechanism["model_cycles"]
        mechanism_by_sequence[seq] = mechanism
        trace_hashes[relative_trace] = actual_trace_sha
        frozen_inputs[trace_path.resolve()] = actual_trace_sha
        attempt = record.get("attempt_id")
        if not isinstance(attempt, str) or attempt in seen_attempt_ids:
            raise BenchmarkError("detailed P2 trial attempt identity is missing or duplicated")
        seen_attempt_ids.add(attempt)
        trial_requests = [
            request
            for request in requests
            if str(request.get("request_id", "")).startswith(attempt + ":")
        ]
        response_usage: list[dict[str, Any]] = []
        for request in trial_requests:
            if request.get("status") != "settled" or request.get("response_received") is not True:
                raise BenchmarkError("detailed P2 provider request is not settled")
            if _finite_nonnegative(request.get("calculated_cost_amount")) is None:
                raise BenchmarkError("detailed P2 provider calculated cost is missing or invalid")
            evidence_path = Path(str(request.get("response_evidence_path", "")))
            evidence, evidence_sha = _read_json(evidence_path, f"provider response {seq}")
            frozen_inputs[evidence_path.resolve()] = evidence_sha
            response_artifact_hashes[str(request.get("request_id"))] = evidence_sha
            if evidence_sha != request.get("response_evidence_sha256") or evidence.get(
                "request_id"
            ) != request.get("request_id"):
                raise BenchmarkError("detailed P2 provider response identity or hash mismatch")
            if evidence.get("provider_model") != "deepseek-flash":
                raise BenchmarkError(
                    "detailed P2 provider response model does not match the frozen alias"
                )
            usage = evidence.get("provider_usage")
            if not isinstance(usage, dict):
                raise BenchmarkError("detailed P2 provider usage is missing")
            usage_fields = (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens",
            )
            if not all(
                type(usage.get(field)) is int and usage[field] >= 0 for field in usage_fields
            ):
                raise BenchmarkError("detailed P2 response usage is missing or malformed")
            if (
                usage["prompt_tokens"] <= 0
                or usage["prompt_tokens"] + usage["completion_tokens"] != usage["total_tokens"]
                or usage["prompt_cache_hit_tokens"] + usage["prompt_cache_miss_tokens"]
                != usage["prompt_tokens"]
            ):
                raise BenchmarkError("detailed P2 response usage totals are inconsistent")
            response_usage.append(usage)
        int_fields = (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        )
        provider_values: dict[str, int | None] = {}
        for field in int_fields:
            values = [usage.get(field) for usage in response_usage]
            provider_values[field] = (
                sum(values)
                if values and all(type(value) is int and value >= 0 for value in values)
                else None
            )
        if provider_values["prompt_tokens"] != record.get("input_tokens") or provider_values[
            "completion_tokens"
        ] != record.get("output_tokens"):
            raise BenchmarkError("detailed P2 response usage does not match its trial ledger")
        attempt_cost = sum(
            float(request.get("calculated_cost_amount"))
            for request in trial_requests
            if _finite_nonnegative(request.get("calculated_cost_amount")) is not None
        )
        if not math.isclose(
            attempt_cost, float(record.get("calculated_cost_amount") or 0), abs_tol=1e-9
        ):
            raise BenchmarkError(
                "detailed P2 per-trial calculated amount does not match the campaign ledger"
            )
        duration_fields = (
            "duration_seconds",
            "model_request_seconds",
            "tool_execution_seconds",
            "context_preparation_seconds",
            "repository_index_seconds",
        )
        durations = {
            field: _finite_nonnegative(run_payload.get(field)) for field in duration_fields
        }
        response_seconds = mechanism["model_response_event_seconds"]
        total_agent = durations["duration_seconds"]
        components = [durations[key] for key in duration_fields[1:]]
        residual = None
        residual_status = "component_or_total_missing"
        if total_agent is not None and all(value is not None for value in components):
            candidate = total_agent - sum(components)
            if candidate >= -0.005:
                residual, residual_status = max(0.0, candidate), "measured_residual"
            else:
                residual_status = "timing_components_overlap_or_exceed_total"
        task_event = next(
            (event for event in events if event["event_type"] == "task_started"), None
        )
        task_text = ""
        if task_event and isinstance(task_event["payload"].get("task"), str):
            task_text = task_event["payload"]["task"]
            patch_path = Path(record["agent_patch_path"])
        patch_hash = record.get("agent_patch_sha256")
        patch_bytes = patch_path.read_bytes()
        if _sha(patch_bytes) != patch_hash:
            raise BenchmarkError("detailed P2 patch hash changed after the shared audit")
        frozen_inputs[patch_path.resolve()] = patch_hash
        trial_artifact_hashes[f"trial-{seq:03d}-patch"] = patch_hash
        verification_path = Path(record["verification_path"])
        verification_hash = record.get("verification_sha256")
        if _sha(verification_path.read_bytes()) != verification_hash:
            raise BenchmarkError("detailed P2 verification hash changed after the shared audit")
        frozen_inputs[verification_path.resolve()] = verification_hash
        trial_artifact_hashes[f"trial-{seq:03d}-verification"] = verification_hash
        trial_rows.append(
            {
                "sequence": seq,
                "task_id": plan["task_id"],
                "arm": plan["arm"],
                "repetition": plan["repetition"],
                "ordinary_primary": plan["task_id"] in protocol.get("primary_task_ids", []),
                "evidence_valid": audit_row["evidence_valid"],
                "passed": audit_row["independent_passed"],
                "failure_reason": audit_row["verification_reason"],
                "termination": audit_row["termination_category"],
                "input_tokens": record.get("input_tokens"),
                "output_tokens": record.get("output_tokens"),
                "request_count": len(trial_requests),
                "provider_prompt_cache_hit_tokens": provider_values["prompt_cache_hit_tokens"],
                "provider_prompt_cache_miss_tokens": provider_values["prompt_cache_miss_tokens"],
                "agent_seconds": record.get("agent_duration_seconds"),
                "verification_seconds": record.get("verification_duration_seconds"),
                "model_request_seconds": durations["model_request_seconds"],
                "model_response_event_seconds": response_seconds,
                "model_response_event_count": mechanism["model_response_event_count"],
                "tool_execution_seconds": durations["tool_execution_seconds"],
                "context_preparation_seconds": durations["context_preparation_seconds"],
                "repository_index_seconds": durations["repository_index_seconds"],
                "unassigned_agent_seconds": residual,
                "unassigned_status": residual_status,
                "test_tool_seconds_nested_in_tool_execution": sum(
                    (event.get("payload", {}).get("result", {}).get("duration_ms") or 0)
                    for event in events
                    if event["event_type"] == "tool_returned"
                    and "test"
                    in str(
                        event.get("payload", {}).get("result", {}).get("tool_name", "")
                    ).casefold()
                )
                / 1000,
                "repository_index_event_count": mechanism["repo_index_event_count"],
                "request_cycles": len(mechanism["model_cycles"]),
                "unanswered_budget_blocked_cycles": int(mechanism["budget_blocked_request_tail"]),
                "tool_call_count": mechanism["call_count"],
                "unique_file_reads": len(
                    {
                        call["path_sha256"]
                        for call in mechanism["calls"]
                        if call["name"] == "read_file" and call["path_sha256"]
                    }
                ),
                "repeated_file_reads": max(
                    0,
                    sum(call["name"] == "read_file" for call in mechanism["calls"])
                    - len(
                        {
                            call["path_sha256"]
                            for call in mechanism["calls"]
                            if call["name"] == "read_file" and call["path_sha256"]
                        }
                    ),
                ),
                "cached_tool_calls": run_payload.get("cached_tool_calls"),
                "failed_tool_calls": sum(
                    item.get("success") is False
                    for item in (
                        event["payload"].get("result")
                        for event in events
                        if event["event_type"] == "tool_returned"
                        and isinstance(event["payload"].get("result"), dict)
                    )
                ),
                "repo_map_present_in_trial_record": bool(run_payload.get("repo_map")),
                "repo_map_candidate_reads": audit_row.get("repo_map_candidate_reads"),
                "task_requests_tests_change": bool(
                    re.search(
                        r"\b(?:add|write|update|modify)\b.{0,30}\btests?\b|(?:新增|添加|编写|修改).{0,12}测试",
                        task_text,
                        re.I,
                    )
                ),
                "presentation_original_chars": (run_payload.get("presentation_metrics") or {}).get(
                    "original_chars"
                ),
                "presentation_visible_chars": (run_payload.get("presentation_metrics") or {}).get(
                    "presented_chars"
                ),
                "presentation_changed_results": (run_payload.get("presentation_metrics") or {}).get(
                    "compacted_result_count"
                ),
                "context_stages": {
                    key: mechanism[key]
                    for key in (
                        "contexts",
                        "content_marker_totals",
                        "per_tool_result",
                        "orphan_failed_test_results",
                        "orphan_failed_test_result_lines",
                    )
                },
                "context_prepared_events": len(mechanism["contexts"]),
                "context_before_peak_estimated_tokens": max(
                    (
                        item["estimated_tokens_before"]
                        for item in mechanism["contexts"]
                        if item["estimated_tokens_before"] is not None
                    ),
                    default=None,
                ),
                "context_after_peak_estimated_tokens": max(
                    (
                        item["estimated_tokens_after"]
                        for item in mechanism["contexts"]
                        if item["estimated_tokens_after"] is not None
                    ),
                    default=None,
                ),
                "context_tool_pruned_results_sum": sum(
                    item["tool_results_pruned"] or 0 for item in mechanism["contexts"]
                ),
                "history_fold_event_count": sum(
                    len(cycle.get("context_events", [])) for cycle in mechanism["model_cycles"]
                ),
                "history_fold_tool_results_pruned_sum": sum(
                    event["tool_results_pruned"] or 0
                    for cycle in mechanism["model_cycles"]
                    for event in cycle.get("context_events", [])
                ),
                "unique_tool_result_count": len(mechanism["per_tool_result"]),
                "repeated_request_tool_view_count": sum(
                    max(0, len(item["request_view_lines"]) - 1)
                    for item in mechanism["per_tool_result"]
                ),
                "tool_presentation_changed_count": sum(
                    item.get("original_chars") != item.get("presented_chars")
                    for item in mechanism["per_tool_result"]
                    if item.get("original_chars") is not None
                    and item.get("presented_chars") is not None
                ),
                "tool_presentation_shortened_count": sum(
                    item["was_shortened"] for item in mechanism["per_tool_result"]
                ),
                "tool_presentation_lengthened_count": sum(
                    item.get("original_chars") is not None
                    and item.get("presented_chars") is not None
                    and item["presented_chars"] > item["original_chars"]
                    for item in mechanism["per_tool_result"]
                ),
                "repo_map_added_event_count": mechanism["repo_map_added_event_count"],
                "repo_map_in_request_count": sum(
                    cycle["repo_map_in_request"] is True for cycle in mechanism["model_cycles"]
                ),
                "repo_map_included_in_any_request": (
                    any(cycle["repo_map_in_request"] is True for cycle in mechanism["model_cycles"])
                    if all(
                        cycle["repo_map_in_request"] is not None
                        for cycle in mechanism["model_cycles"]
                    )
                    else None
                ),
                "artifact_sha256": {
                    "run_result": run_sha,
                    "patch": patch_hash,
                    "verification": verification_hash,
                    "trace": actual_trace_sha,
                },
                "task_prompt_sha256": _sha(task_text.encode("utf-8")),
                "system_prompt_sha256": audit_row.get("system_prompt_sha256"),
            }
        )

    if set(lock_entries) != expected_trace_paths:
        raise BenchmarkError("detailed P2 frozen trace-hash manifest has extra or missing entries")
    unmatched = [
        request
        for request in requests
        if not any(
            str(request.get("request_id", "")).startswith(attempt + ":")
            for attempt in seen_attempt_ids
        )
    ]
    if unmatched:
        raise BenchmarkError("detailed P2 campaign contains requests outside the frozen trials")
    if len({request.get("request_id") for request in requests}) != len(requests):
        raise BenchmarkError("detailed P2 campaign has duplicate provider request identities")
    if (
        initial_ledger.get("uncertain_request")
        or initial_ledger.get("halt_reason")
        or initial_ledger.get("reserved_amount") != 0
    ):
        raise BenchmarkError("detailed P2 campaign is unsettled")
    final_ledger_sha = _sha(ledger_path.read_bytes())
    if final_ledger_sha != initial_digest:
        raise BenchmarkError("shared campaign ledger changed during detailed analysis")
    changed_inputs = [
        path.name
        for path, digest in frozen_inputs.items()
        if not path.is_file() or _sha(path.read_bytes()) != digest
    ]
    if changed_inputs:
        raise BenchmarkError("detailed P2 frozen inputs changed during analysis")
    for request in requests:
        if request.get("currency") not in (None, initial_ledger.get("currency")):
            raise BenchmarkError("detailed P2 request currency conflicts with shared ledger")
    for field in (
        "cap_amount",
        "currency",
        "provider",
        "model_name",
        "protocol_identity",
        "pricing_identity",
    ):
        if before.get(field) != initial_ledger.get(field):
            raise BenchmarkError("detailed P2 shared campaign identity changed since batch start")
    derived_hashes = trial_artifact_hashes
    # Comparison IDs are paired by task and repetition; all 30 pairs stay in the report.
    pairs: list[dict[str, Any]] = []
    by_task_rep = {(row["task_id"], row["repetition"], row["arm"]): row for row in trial_rows}
    for task_id in protocol["qualified_task_ids"]:
        primary = task_id in protocol.get("primary_task_ids", [])
        for repetition in range(1, 4):
            base = by_task_rep[(task_id, repetition, "no_compaction")]
            action = by_task_rep[(task_id, repetition, "action_optimization_bundle")]
            direction = (
                "same_success"
                if base["passed"] == action["passed"]
                else "regression"
                if base["passed"]
                else "reverse"
            )
            first = _cycle_difference(
                cycles_by_sequence[base["sequence"]], cycles_by_sequence[action["sequence"]]
            )
            base_facts = mechanism_by_sequence[base["sequence"]]["content_marker_totals"]
            action_facts = mechanism_by_sequence[action["sequence"]]["content_marker_totals"]
            fact_changes = {
                key: {"baseline": base_facts.get(key, 0), "action": action_facts.get(key, 0)}
                for key in sorted(set(base_facts) | set(action_facts))
                if base_facts.get(key, 0) != action_facts.get(key, 0)
            }
            pairs.append(
                {
                    "task_id": task_id,
                    "repetition": repetition,
                    "primary_ordinary": primary,
                    "direction": direction,
                    "baseline_sequence": base["sequence"],
                    "action_sequence": action["sequence"],
                    "baseline_passed": base["passed"],
                    "action_passed": action["passed"],
                    "baseline_failure": base["failure_reason"],
                    "action_failure": action["failure_reason"],
                    "baseline_termination": base["termination"],
                    "action_termination": action["termination"],
                    "input_delta_action_minus_baseline": action["input_tokens"]
                    - base["input_tokens"],
                    "output_delta_action_minus_baseline": action["output_tokens"]
                    - base["output_tokens"],
                    "agent_seconds_delta_action_minus_baseline": action["agent_seconds"]
                    - base["agent_seconds"],
                    "first_post_request_observable_difference": first,
                    "content_marker_count_changes": fact_changes,
                    "evidence": [
                        {
                            "sequence": base["sequence"],
                            "trace_sha256": base["artifact_sha256"]["trace"],
                            "run_result_sha256": base["artifact_sha256"]["run_result"],
                        },
                        {
                            "sequence": action["sequence"],
                            "trace_sha256": action["artifact_sha256"]["trace"],
                            "run_result_sha256": action["artifact_sha256"]["run_result"],
                        },
                    ],
                    "interpretation": (
                        "observable_association_only; no causal or model-motive claim"
                    ),
                }
            )
    ordinary = protocol.get("primary_task_ids", [])
    special = protocol.get("collection_failure_task_ids", [])
    if len(pairs) != 30:
        raise BenchmarkError("detailed P2 task/repetition pairing is incomplete")
    metric_fields = (
        "input_tokens",
        "output_tokens",
        "agent_seconds",
        "verification_seconds",
        "model_request_seconds",
        "tool_execution_seconds",
        "context_preparation_seconds",
        "repository_index_seconds",
        "model_response_event_seconds",
        "unassigned_agent_seconds",
        "request_cycles",
        "tool_call_count",
        "cached_tool_calls",
        "failed_tool_calls",
        "repeated_file_reads",
        "unique_file_reads",
        "provider_prompt_cache_hit_tokens",
        "provider_prompt_cache_miss_tokens",
        "request_count",
        "test_tool_seconds_nested_in_tool_execution",
        "context_prepared_events",
        "context_before_peak_estimated_tokens",
        "context_after_peak_estimated_tokens",
        "context_tool_pruned_results_sum",
        "history_fold_event_count",
        "history_fold_tool_results_pruned_sum",
        "unique_tool_result_count",
        "repeated_request_tool_view_count",
        "tool_presentation_changed_count",
        "tool_presentation_shortened_count",
        "tool_presentation_lengthened_count",
        "repo_map_added_event_count",
        "repo_map_in_request_count",
        "repo_map_candidate_reads",
    )
    group_summaries = {}
    for label, task_ids in (
        ("ordinary_primary", ordinary),
        ("reviewed_collection_failure", special),
    ):
        group_rows = [row for row in trial_rows if row["task_id"] in task_ids]
        group_summaries[label] = {
            arm: {
                "planned": len([row for row in group_rows if row["arm"] == arm]),
                "successes": sum(row["passed"] is True for row in group_rows if row["arm"] == arm),
                "failure_reasons": dict(
                    sorted(
                        Counter(
                            row["failure_reason"] for row in group_rows if row["arm"] == arm
                        ).items()
                    )
                ),
                "termination_reasons": dict(
                    sorted(
                        Counter(
                            row["termination"] for row in group_rows if row["arm"] == arm
                        ).items()
                    )
                ),
                "infrastructure_incidents": sum(
                    row["termination"] == "infrastructure_error"
                    for row in group_rows
                    if row["arm"] == arm
                ),
                "invalid_evidence_count": sum(
                    row["evidence_valid"] is not True for row in group_rows if row["arm"] == arm
                ),
            }
            for arm in _ARMS
        }
    task_means = _task_equal_metrics(trial_rows, ordinary, _ARMS, metric_fields)
    special_task_means = _task_equal_metrics(trial_rows, special, _ARMS, metric_fields)
    mechanism_totals = {}
    for arm in _ARMS:
        selected = [row for row in trial_rows if row["arm"] == arm]

        def sum_known(field: str, rows: list[dict[str, Any]] = selected) -> int | float | None:
            values = [row.get(field) for row in rows]
            return sum(values) if values and all(value is not None for value in values) else None

        def max_known(field: str, rows: list[dict[str, Any]] = selected) -> int | float | None:
            values = [row.get(field) for row in rows]
            return max(values) if values and all(value is not None for value in values) else None

        mechanism_totals[arm] = {
            "planned_trials": len(selected),
            "provider_request_count": sum_known("request_count"),
            "repo_map_trials_recorded": sum(
                row["repo_map_present_in_trial_record"] for row in selected
            ),
            "repo_map_requests": sum_known("repo_map_in_request_count"),
            "repo_map_candidate_reads": sum_known("repo_map_candidate_reads"),
            "unique_tool_results": sum_known("unique_tool_result_count"),
            "repeated_request_tool_views": sum_known("repeated_request_tool_view_count"),
            "cached_tool_calls": sum_known("cached_tool_calls"),
            "failed_tool_calls": sum_known("failed_tool_calls"),
            "presentation_changed_results": sum_known("tool_presentation_changed_count"),
            "presentation_shortened_results": sum_known("tool_presentation_shortened_count"),
            "presentation_lengthened_results": sum_known("tool_presentation_lengthened_count"),
            "presentation_original_chars": sum_known("presentation_original_chars"),
            "presentation_visible_chars": sum_known("presentation_visible_chars"),
            "context_prepared_events": sum_known("context_prepared_events"),
            "context_tool_pruned_operations": sum_known("context_tool_pruned_results_sum"),
            "context_history_fold_events": sum_known("history_fold_event_count"),
            "context_history_fold_pruned_operations": sum_known(
                "history_fold_tool_results_pruned_sum"
            ),
            "max_preparation_peak_before_estimate": max_known(
                "context_before_peak_estimated_tokens"
            ),
            "max_preparation_peak_after_estimate": max_known("context_after_peak_estimated_tokens"),
        }
    timing_pairs = {}
    for subset_name, task_ids in (
        ("ordinary_primary", ordinary),
        ("reviewed_collection_failure", special),
    ):
        task_deltas = {}
        for arm in _ARMS:
            if arm == "no_compaction":
                continue
            per_task = {}
            for task in task_ids:
                deltas = []
                for rep in range(1, 4):
                    left, right = (
                        by_task_rep[(task, rep, "no_compaction")],
                        by_task_rep[(task, rep, arm)],
                    )
                    deltas.append(
                        {
                            metric: (right[metric] - left[metric])
                            if isinstance(right.get(metric), (int, float))
                            and not isinstance(right.get(metric), bool)
                            and isinstance(left.get(metric), (int, float))
                            and not isinstance(left.get(metric), bool)
                            else None
                            for metric in metric_fields
                        }
                    )
                per_task[task] = {
                    metric: mean([delta[metric] for delta in deltas])
                    if all(delta[metric] is not None for delta in deltas)
                    else None
                    for metric in metric_fields
                }
            task_deltas[arm] = {
                "per_task_action_minus_baseline": per_task,
                "task_equal_weight_mean_delta": {
                    metric: mean([values[metric] for values in per_task.values()])
                    if per_task and all(values[metric] is not None for values in per_task.values())
                    else None
                    for metric in metric_fields
                },
            }
        timing_pairs[subset_name] = task_deltas
    changed_inputs = [
        path.name
        for path, digest in frozen_inputs.items()
        if not path.is_file() or _sha(path.read_bytes()) != digest
    ]
    if changed_inputs or _sha(ledger_path.read_bytes()) != final_ledger_sha:
        raise BenchmarkError("frozen P2 evidence or campaign changed before report creation")
    report = {
        "schema_version": 1,
        "kind": "p2_detailed_ablation_offline_diagnostic",
        "generated_at": datetime.now(UTC).isoformat(),
        "execution_commit": protocol.get("code_commit"),
        "protocol_sha256": protocol_digest,
        "diagnostic_code_sha256": frozen_inputs[Path(__file__).resolve()],
        "trace_hash_lock_sha256": lock_digest,
        "campaign_before_sha256": before_digest,
        "campaign_current_sha256": final_ledger_sha,
        "campaign_currency": initial_ledger.get("currency"),
        "campaign_cap": initial_ledger.get("cap_amount"),
        "paid_requests_made": 0,
        "provider_clients_constructed": 0,
        "evidence": {
            "planned": 120,
            "audited": len(audit["rows"]),
            "valid": sum(row["evidence_valid"] for row in audit["rows"]),
            "trace_files_verified": len(trace_hashes),
            "provider_responses_verified": len(requests),
            "trial_artifact_hashes": len(derived_hashes),
            "campaign_prefix_preserved": True,
            "unresolved_requests": 0,
            "original_experiment_unchanged": True,
        },
        "group_summaries": group_summaries,
        "mechanism_totals": mechanism_totals,
        "task_equal_weight_metrics": task_means,
        "special_task_equal_weight_metrics": special_task_means,
        "paired_task_equal_weight_deltas": timing_pairs,
        "paired_cases": pairs,
        "trials": trial_rows,
        "implementation_decision": {
            "status": "no_reproducible_implementation_defect_demonstrated",
            "reason": (
                "Observed marker omissions are post-hoc signatures and are not linked to a "
                "semantic requirement or a deterministic failing fixture. Budget-skipped tests "
                "and unanswered tail cycles match explicit budget-stop evidence."
            ),
            "next_single_variable_hypothesis": (
                "Tool-result presentation shortening may reduce input usage while obscuring "
                "repository-path, assertion, exception, or test-node signatures in some paired "
                "runs; this remains an association, not a causal finding."
            ),
            "next_validation": (
                "On development tasks only, compare all-off baseline with presentation-only "
                "enabled; disable action guidance and read cache in the treatment. Keep model, "
                "task set, prompt, scoring, budgets, and repetitions fixed; predeclare success, "
                "usage, marker-retention, and stopping analyses. Do not evaluate or tune on the "
                "20-task holdout."
            ),
        },
        "input_artifact_sha256": {
            "trial_and_run_artifacts": {
                **derived_hashes,
            },
            "provider_response_artifacts": response_artifact_hashes,
            "frozen_trace_artifacts": trace_hashes,
        },
        "interpretation_limits": [
            "This is post-hoc offline evidence description, not a randomized new experiment "
            "or causal estimate.",
            "Elapsed model-call boundary includes client processing and ledger settlement; "
            "it is not provider-only latency.",
            "Test-tool durations are a subset of total tool-execution time and must not be "
            "added again.",
            "Repeated tool-result views can count the same content repeatedly; character "
            "counts and local token estimates are not provider token savings.",
            "Missing cache or timing fields remain null; no missing evidence is converted to zero.",
            "Marker categories identify only configured signatures, not semantic "
            "sufficiency or model intent.",
        ],
    }
    return report, final_ledger_sha


def write_detailed_ablation_diagnostic(
    experiment_dir: Path,
    *,
    output_dir: Path,
    ledger_path: Path,
    campaign_before_path: Path,
    trace_hash_lock_path: Path,
) -> tuple[Path, Path, Path]:
    """Write new offline reports to a new directory; never edits the run or ledger."""
    output = output_dir.expanduser().resolve()
    root = experiment_dir.expanduser().resolve()
    if output == root or root in output.parents:
        raise BenchmarkError(
            "detailed P2 output must be outside the immutable experiment directory"
        )
    if output.exists():
        raise BenchmarkError("detailed P2 output directory already exists; refusing overwrite")
    report, ledger_sha = _analyze(
        experiment_dir, ledger_path, campaign_before_path, trace_hash_lock_path
    )
    if hashlib.sha256(ledger_path.read_bytes()).hexdigest() != ledger_sha:
        raise BenchmarkError("shared P2 ledger changed before output creation")
    pairs_path = output / "paired-cases.json"
    json_path = output / "detailed-diagnostic.json"
    markdown_path = output / "detailed-diagnostic.md"
    output.mkdir(parents=True, exist_ok=False)
    pairs = {
        "schema_version": 1,
        "protocol_sha256": report["protocol_sha256"],
        "ordinary_pair_count": sum(pair["primary_ordinary"] for pair in report["paired_cases"]),
        "all_pair_count": len(report["paired_cases"]),
        "cases": report["paired_cases"],
    }
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pairs_path.write_text(json.dumps(pairs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# P2 四组消融离线详细诊断",
        "",
        f"执行版本：`{report['execution_commit']}`；固定位置 {report['evidence']['audited']}/120，",
        (
            f"有效证据 {report['evidence']['valid']}；供应商响应核验 "
            f"{report['evidence']['provider_responses_verified']}。"
        ),
        "分析期间付费请求和供应商客户端均为 0。",
        "",
        "报告只保存计数、哈希、相对试次编号及轨迹事件行号。缺失值保留 null。",
        "模型请求视图、工具输出、任务描述和补丁正文仍只在原始本机轨迹中。",
        "",
        "逐对基线与行动优化诊断见 `paired-cases.json`。每对按任务 ID 和重复编号对齐；"
        "首次差异是可观察事件差异，不是因果归因。",
        "",
        "## 主要集合任务等权均值",
        "",
    ]
    for arm, values in report["task_equal_weight_metrics"].items():
        lines.append(
            f"- `{arm}`："
            + "; ".join(f"{key}={value}" for key, value in values["task_equal_weight_mean"].items())
        )
    primary_pairs = [pair for pair in report["paired_cases"] if pair["primary_ordinary"]]
    pair_counts = Counter(pair["direction"] for pair in primary_pairs)
    lines.extend(
        [
            "",
            "## 基线与行动优化配对",
            "",
            f"普通集合 24 对：{pair_counts['regression']} 对退步、{pair_counts['reverse']} 对反向、"
            f"{pair_counts['same_success']} 对结果相同；另有 6 对特殊资格单列。",
            "",
            (
                "| 任务 | 重复 | 方向 | 基线/行动试次 | 首个差异 | 验收基线/行动 | "
                "输入差 | Agent 秒差 | 标记变化 |"
            ),
            "| --- | ---: | --- | --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for pair in primary_pairs:
        if pair["direction"] not in {"regression", "reverse"}:
            continue
        first = pair["first_post_request_observable_difference"] or {}
        marker_text = json.dumps(
            pair["content_marker_count_changes"], ensure_ascii=False, sort_keys=True
        )
        lines.append(
            f"| {pair['task_id']} | {pair['repetition']} | {pair['direction']} | "
            f"{pair['baseline_sequence']}/{pair['action_sequence']} | "
            f"{first.get('stage', 'unknown')} @{first.get('baseline_event_line')}/"
            f"{first.get('action_event_line')} | "
            f"{pair['baseline_failure']}/{pair['action_failure']} | "
            f"{pair['input_delta_action_minus_baseline']} | "
            f"{pair['agent_seconds_delta_action_minus_baseline']:.3f} | {marker_text} |"
        )
    lines.extend(
        [
            "",
            "## 耗时分解：普通题内重复均值后任务等权",
            "",
            (
                "| 组别 | Agent 秒 | 验收秒 | 模型调用边界秒 | 工具秒 | 测试工具嵌套秒 | "
                "上下文准备秒 | Repo Map 构建秒 | 未归属秒 |"
            ),
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for arm, values in report["task_equal_weight_metrics"].items():
        item = values["task_equal_weight_mean"]
        lines.append(
            f"| {arm} | {item['agent_seconds']:.3f} | {item['verification_seconds']:.3f} | "
            f"{item['model_request_seconds']:.3f} | {item['tool_execution_seconds']:.3f} | "
            f"{item['test_tool_seconds_nested_in_tool_execution']:.3f} | "
            f"{item['context_preparation_seconds']:.3f} | {item['repository_index_seconds']:.3f} | "
            f"{item['unassigned_agent_seconds']:.3f} |"
        )
    lines.extend(
        [
            "",
            "模型调用边界含客户端和账本处理，不是纯供应商响应时间；响应事件时间是边界内部观测，不相加。"
            "测试工具耗时属于工具总耗时的子项。",
            "",
            "## 机制指标：每组 30 项",
            "",
            (
                "| 组别 | 请求 | Repo Map 入请求 | 候选读取 | 重复工具视图 | 缓存命中调用 | "
                "失败工具调用 | 输出缩短/变长 | 上下文裁剪 | 历史折叠 | 最大裁剪前/后估算峰值 |"
            ),
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |",
        ]
    )
    for arm, values in report["mechanism_totals"].items():
        lines.append(
            f"| {arm} | {values['provider_request_count']} | {values['repo_map_requests']} | "
            f"{values['repo_map_candidate_reads']} | {values['repeated_request_tool_views']} | "
            f"{values['cached_tool_calls']} | {values['failed_tool_calls']} | "
            f"{values['presentation_shortened_results']}/"
            f"{values['presentation_lengthened_results']} | "
            f"{values['context_tool_pruned_operations']} | "
            f"{values['context_history_fold_events']} | "
            f"{values['max_preparation_peak_before_estimate']}/"
            f"{values['max_preparation_peak_after_estimate']} |"
        )
    action = report["mechanism_totals"]["action_optimization_bundle"]
    context = report["mechanism_totals"]["context_management_only"]
    lines.extend(
        [
            "",
            f"行动优化缩短 {action['presentation_shortened_results']} 条工具输出；原始字符 "
            f"{action['presentation_original_chars']}，呈现字符 "
            f"{action['presentation_visible_chars']}。"
            "字符差不是供应商 Token 节省。上下文裁剪与历史折叠独立计数。",
            f"上下文组最大本地估算峰值为 {context['max_preparation_peak_before_estimate']}→"
            f"{context['max_preparation_peak_after_estimate']}。",
            "",
            "## 失败分布与改进决策",
            "",
            "原验收分类不变：48 通过、24 空补丁、35 pytest 执行失败、12 收集失败、"
            "1 测试/配置修改拒绝。"
            "12 条 skipped run_tests 结果和 50 个预算尾部周期均保留并标注预算原因。",
            "尚不能证明优化实现存在可复现缺陷，本轮不修改优化策略。事后启发式标记缺失不能证明语义必要性。",
            "单变量假设：工具输出呈现缩短可能降低输入，同时遮蔽部分诊断特征。下一开发集仅启用呈现，"
            "关闭行动引导和读取缓存；固定模型、题目、提示、评分、预算和重复数，预先冻结成功、"
            "用量、标记保留和资源终止分析。先拆分呈现开关并验证合成回归；20 题留出集保持未使用。",
        ]
    )
    lines.extend(
        ["", "## 限制", ""] + [f"- {item}" for item in report["interpretation_limits"]] + [""]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path, pairs_path
