"""Offline evidence analysis; experimental projections never alter runtime defaults."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from tracefix.context import ContextConfig, ContextManager
from tracefix.messages import Message
from tracefix.models.base import LLMConfig
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.tools.base import ToolResult, ToolSpec


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def file_sha(path: Path) -> str:
    return digest(path.read_bytes())


def load_events(path: Path) -> tuple[list[dict], list[str]]:
    events, errors = [], []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
                raise ValueError("invalid event")
            if "event_type" not in event or "step" not in event:
                raise ValueError("missing event identity")
            events.append(event)
        except ValueError:
            errors.append(f"malformed_event_line_{number}")
    return events, errors


def valid_usage(value) -> bool:
    return (
        isinstance(value, dict)
        and all(
            type(value.get(key)) is int and value[key] >= 0
            for key in ("input_tokens", "output_tokens", "total_tokens")
        )
        and value["total_tokens"] == value["input_tokens"] + value["output_tokens"]
    )


def ranges(numbers) -> list[list[int]]:
    result = []
    for number in sorted(set(numbers)):
        if result and number == result[-1][1] + 1:
            result[-1][1] = number
        else:
            result.append([number, number])
    return result


def lines(text: str) -> dict[int, str]:
    return {
        int(match[1]): match[2]
        for line in text.splitlines()
        if (match := re.fullmatch(r"\s*(\d+) \| (.*)", line))
    }


def visible_lines(message: Message, raw: dict[int, str]) -> set[int] | None:
    try:
        output = json.loads(message.content or "")["output"]
        if (
            not isinstance(output, dict)
            or "content" not in output
            or output.get("_tracefix_pruned")
        ):
            return None
        return {n for n, value in lines(output["content"]).items() if raw.get(n) == value}
    except (ValueError, KeyError, TypeError):
        return None


def line_page(result: ToolResult, limit: int = 4000) -> dict:
    """Head/tail complete-line pages with explicit gaps, using returned data only."""
    output = dict(result.output)
    content = output.get("content", "")
    numbered = list(lines(content).items())
    selected = {}
    for entries, budget in ((numbered, limit * 3 // 4), (numbered[::-1], limit // 4)):
        used = 0
        for number, value in entries:
            row = f"{number:>6} | {value}"
            if used + len(row) + 1 > budget:
                break
            selected[number] = value
            used += len(row) + 1
    omitted = set(dict(numbered)) - set(selected)
    output.update(
        content="\n".join(f"{n:>6} | {v}" for n, v in sorted(selected.items())),
        visible_ranges=ranges(selected),
        omitted_ranges=ranges(omitted),
        next_start_line=min(omitted)
        if omitted
        else (
            output.get("end_line", 0) + 1
            if output.get("end_line", 0) < output.get("total_lines", 0)
            else None
        ),
        evidence_kind="observed_tool_lines_not_verified_full_file",
    )
    return {
        "call_id": result.call_id,
        "tool_name": result.tool_name,
        "success": result.success,
        "output": output,
    }


def adapter_for(config: dict) -> LiteLLMAdapter:
    return LiteLLMAdapter(
        LLMConfig(
            model_name=config["model_name"],
            timeout_seconds=config["llm_timeout_seconds"],
            max_retries=config["llm_max_retries"],
            max_output_tokens=config["per_request_output_tokens"],
            extra_kwargs={
                "api_base": "https://api.deepseek.com",
                "extra_body": {"thinking": {"type": "disabled"}},
            },
        ),
        client=object(),
    )


def wire(messages, tools, adapter):
    return adapter.request_kwargs(messages, tools)


def same_view(first, second, adapter, tools) -> bool:
    return wire(first, tools, adapter) == wire(second, tools, adapter)


def tool_pairing(messages) -> bool:
    pending = []
    seen = set()
    for message in messages:
        if message.tool_calls:
            if pending or any(call.id in seen for call in message.tool_calls):
                return False
            pending = [call.id for call in message.tool_calls]
            seen.update(pending)
        elif message.role.value == "tool":
            if message.tool_call_id not in pending:
                return False
            pending.remove(message.tool_call_id)
        elif pending:
            return False
    return not pending


def components(messages, tools, adapter) -> dict:
    totals = Counter()
    for message in messages:
        if message.metadata.get("kind") == "task_fact_anchor":
            category = "facts"
        elif message.metadata.get("tracefix_context_summary"):
            category = "summary"
        elif message.role.value in ("system", "user") and not message.tool_call_id:
            category = "task_and_guidance"
        elif message.role.value == "tool":
            try:
                name = json.loads(message.content or "").get("tool_name")
            except ValueError:
                name = None
            category = {
                "read_file": "source",
                "run_tests": "tests",
                "apply_patch": "patch_and_diff",
                "get_git_diff": "patch_and_diff",
            }.get(name, "other_history")
        else:
            category = "other_history"
        totals[category] += len(encoded(adapter._format_message(message)))
    totals["tool_schema"] = len(encoded([t.to_openai_tool() for t in tools]))
    return dict(totals)


def classify_read(raw, previously_returned, previously_visible, ever_visible=None):
    if not raw:
        return "cannot_judge", 0, 0
    returned_overlap = raw & previously_returned
    visible_overlap = raw & previously_visible
    ever_visible = previously_visible if ever_visible is None else ever_visible
    if returned_overlap - ever_visible:
        kind = "necessary_reread_of_previously_hidden_lines"
    elif returned_overlap - previously_visible:
        kind = "necessary_reread_after_context_eviction"
    elif raw and raw <= previously_visible:
        kind = "fully_previously_visible"
    elif returned_overlap:
        kind = "partial_overlap"
    else:
        kind = "new_range"
    return kind, len(returned_overlap), len(visible_overlap)


def preserved(original, candidate, raw_results):
    reasons = []
    if not tool_pairing(candidate):
        reasons.append("invalid_tool_pairing")
    candidate_by_call = {m.tool_call_id: m for m in candidate if m.tool_call_id}
    for message in original:
        if message.role.value == "system" or message.metadata.get("kind") == "task_fact_anchor":
            if not any(m.role == message.role and m.content == message.content for m in candidate):
                reasons.append("lost_system_or_fact_anchor")
        if message.tool_call_id in raw_results:
            result = raw_results[message.tool_call_id]
            if result.tool_name == "read_file" and isinstance(result.output, dict):
                original_visible = visible_lines(message, lines(result.output.get("content", "")))
                other = candidate_by_call.get(message.tool_call_id)
                candidate_visible = (
                    visible_lines(other, lines(result.output.get("content", "")))
                    if other
                    else set()
                )
                if original_visible is None or candidate_visible is None:
                    reasons.append("read_visibility_unverifiable")
                elif original_visible - candidate_visible:
                    reasons.append("lost_previously_visible_source_lines")
    # The first task and all current gate feedback are mandatory, not mere summaries.
    user = next((m for m in original if m.role.value == "user"), None)
    if user and not any(m.content == user.content for m in candidate):
        reasons.append("lost_task_contract")
    for message in original:
        if "自动验收未通过" in (message.content or "") and not any(
            m.content == message.content for m in candidate
        ):
            reasons.append("lost_gate_feedback")
    # Latest noncached test result is conservative evidence for fixed-action replay.
    tests = [
        m
        for m in original
        if m.tool_call_id in raw_results and raw_results[m.tool_call_id].tool_name == "run_tests"
    ]
    if tests and not any(m.content == tests[-1].content for m in candidate):
        reasons.append("lost_latest_test_result")
    patches = [
        m
        for m in original
        if m.tool_call_id in raw_results and raw_results[m.tool_call_id].tool_name == "apply_patch"
    ]
    if patches and not any(m.content == patches[-1].content for m in candidate):
        reasons.append("lost_latest_patch_result")
    return sorted(set(reasons))


def analyze_run(record: dict, campaign: Path, ledger: list[dict]) -> dict:
    stage = record["stage"]
    config_path = campaign / stage / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    run = Path(record["result_path"]).parent
    events_path = run / "trajectory.jsonl"
    events, errors = load_events(events_path)
    final = json.loads((run / "result.json").read_text(encoding="utf-8"))
    if record.get("config_sha256") != file_sha(config_path):
        errors.append("config_hash_mismatch")
    if record.get("source", {}).get("commit", final.get("source_commit")) != final.get(
        "source_commit"
    ):
        errors.append("source_commit_mismatch")
    adapter = adapter_for(config)
    context = ContextConfig.model_validate(config["agent_config"]["context"])
    history, requests, reads = [], [], []
    raw_results, calls, raw_before, visible_before = {}, {}, {}, {}
    ever_visible = {}
    all_seen_results = set()
    epoch, applied = 0, []
    current_request = None
    used_input = 0
    usage_complete = True
    ledger_cursor = 0
    mapped = []
    for index, event in enumerate(events):
        kind, payload, step = event["event_type"], event["payload"], event["step"]
        if kind == "tool_called":
            call = payload["call"]
            if calls.get(call["id"], {}).get("executed"):
                errors.append("duplicate_tool_call_id")
            calls[call["id"]] = {"call": call, "step": step, "epoch": epoch, "executed": True}
        elif kind == "tool_returned":
            result = ToolResult.model_validate(payload["result"])
            if result.call_id not in calls or result.call_id in all_seen_results:
                errors.append("unpaired_or_duplicate_tool_result")
            elif not calls[result.call_id].get("executed") and not result.metadata.get("skipped"):
                errors.append("tool_result_without_execution_or_explicit_skip")
            if (
                result.call_id in calls
                and calls[result.call_id]["call"]["name"] != result.tool_name
            ):
                errors.append("tool_result_name_mismatch")
            all_seen_results.add(result.call_id)
            raw_results[result.call_id] = result
            if result.tool_name == "apply_patch" and result.success:
                epoch += 1
                applied.append(result.call_id)
            if (
                result.tool_name == "read_file"
                and result.success
                and isinstance(result.output, dict)
            ):
                output = result.output
                raw = set(lines(output.get("content", "")))
                key = (epoch, output.get("path"))
                classification, overlap, visible_overlap = classify_read(
                    raw,
                    raw_before.get(key, set()),
                    visible_before.get(key, set()),
                    ever_visible.get(key, set()),
                )
                reads.append(
                    {
                        "step": step,
                        "call_id": result.call_id,
                        "path": output.get("path"),
                        "requested": calls.get(result.call_id, {}).get("call", {}).get("arguments"),
                        "returned_bounds": [output.get("start_line"), output.get("end_line")],
                        "observed_returned_ranges": ranges(raw),
                        "classification": classification,
                        "overlap_lines": overlap,
                        "previously_visible_overlap_lines": visible_overlap,
                        "previously_hidden_overlap_lines": len(
                            (raw & raw_before.get(key, set())) - ever_visible.get(key, set())
                        ),
                        "evicted_overlap_lines": len(
                            (raw & ever_visible.get(key, set())) - visible_before.get(key, set())
                        ),
                        "tool_truncated": output.get("truncated"),
                        "change_epoch": epoch,
                        "source_identity": {
                            "commit": final.get("source_commit"),
                            "successful_patch_call_ids": list(applied),
                        },
                        "identity_kind": "recorded_change_chain_not_full_file_hash",
                        "returned_content_sha256": digest(output.get("content", "").encode()),
                        "visible_ranges_by_request": [],
                    }
                )
                raw_before.setdefault(key, set()).update(raw)
        elif kind == "message_added":
            message = Message.model_validate(payload["message"])
            for call in message.tool_calls:
                calls.setdefault(
                    call.id,
                    {
                        "call": call.model_dump(mode="json"),
                        "step": step,
                        "epoch": epoch,
                        "executed": False,
                    },
                )
            history.append(message)
        elif kind == "model_request_view":
            messages = tuple(Message.model_validate(m) for m in payload["messages"])
            tools = tuple(ToolSpec.model_validate(t) for t in payload["tools"])
            kwargs = wire(messages, tools, adapter)
            request_sha = digest(encoded(kwargs))
            entry = ledger[ledger_cursor] if ledger_cursor < len(ledger) else None
            if entry and entry.get("request_sha256") != request_sha:
                entry = None
            if entry:
                mapped.append(request_sha)
            anchors = [m for m in messages if m.metadata.get("kind") == "task_fact_anchor"]
            for anchor in anchors:
                try:
                    fact = json.loads((anchor.content or "").split("\n", 1)[1])["latest_test"]
                    evidence = raw_results[fact["call_id"]]
                    if evidence.tool_name != "run_tests" or evidence.success != fact["success"]:
                        errors.append("fact_anchor_tool_mismatch")
                    if fact.get("valid") and not evidence.success:
                        errors.append("invalid_verified_fact_anchor")
                except (ValueError, KeyError, IndexError, TypeError):
                    errors.append("fact_anchor_unverifiable")
            replay_history = tuple(history)
            if anchors and history:
                replay_history = (history[0], *anchors, *history[1:])
            view = ContextManager(context).prepare(replay_history, tools)
            replay_ok = same_view(view.messages, messages, adapter, tools)
            if not replay_ok:
                errors.append(f"step_{step}_baseline_view_mismatch")
            if not tool_pairing(messages):
                errors.append(f"step_{step}_invalid_request_tool_pairing")
            row = {
                "step": step,
                "event_index": index,
                "request_sha256": request_sha,
                "hash_verified": bool(entry),
                "baseline_reproduced": replay_ok,
                "tool_pairing_valid": tool_pairing(messages),
                "production_estimate": payload["context"]["estimated_tokens_after"],
                "wire_bytes": len(encoded(kwargs)),
                "byte_reservation": adapter.count_input_tokens(messages, tools),
                "observed_usage": entry.get("usage") if entry else None,
                "ledger_status": entry.get("status") if entry else None,
                "cumulative_input_before": used_input,
                "components_bytes": components(messages, tools, adapter),
                "component_estimates_utf8_bytes_div_four": {
                    name: (size + 3) // 4
                    for name, size in components(messages, tools, adapter).items()
                },
                "recorded_context": payload["context"],
                "local_count": adapter.count_input_bound(messages, tools).__dict__,
                "candidates": [],
            }
            if entry and (
                entry.get("status") != "completed" or not valid_usage(entry.get("usage"))
            ):
                errors.append(f"step_{step}_usage_unknown_or_invalid")
                usage_complete = False
            adapter._counted_request_sha256 = None
            if entry and valid_usage(entry.get("usage")):
                used_input += entry["usage"]["input_tokens"]
                if row["byte_reservation"] != entry.get("input_bound"):
                    errors.append(f"step_{step}_byte_reservation_mismatch")
            row["cumulative_input_after"] = used_input if usage_complete else None
            row["usage_complete"] = bool(entry and valid_usage(entry.get("usage")))
            visible_before = {}
            for read in reads:
                message = next((m for m in messages if m.tool_call_id == read["call_id"]), None)
                raw_result = raw_results[read["call_id"]]
                known = (
                    visible_lines(message, lines(raw_result.output.get("content", "")))
                    if message
                    else set()
                )
                read["visible_ranges_by_request"].append(
                    {"step": step, "ranges": ranges(known) if known is not None else None}
                )
                if known is not None:
                    visible_before.setdefault((read["change_epoch"], read["path"]), set()).update(
                        known
                    )
                    ever_visible.setdefault((read["change_epoch"], read["path"]), set()).update(
                        known
                    )
            if replay_ok and row["tool_pairing_valid"] and usage_complete:
                variants = []
                paged = []
                for message in replay_history:
                    result = raw_results.get(message.tool_call_id)
                    if result and result.tool_name == "read_file" and result.success:
                        paged.append(
                            message.model_copy(
                                update={
                                    "content": json.dumps(
                                        line_page(result), ensure_ascii=False, separators=(",", ":")
                                    )
                                }
                            )
                        )
                    else:
                        paged.append(message)
                variants.append(
                    ("line_pages", ContextManager(context).prepare(paged, tools).messages)
                )
                for name, threshold in (
                    ("threshold_6000", 6000),
                    (
                        "remaining_budget_context",
                        min(6000, max(2000, (60000 - row["cumulative_input_before"]) // 4)),
                    ),
                ):
                    candidate_config = context.model_copy(
                        update={"compaction_trigger_tokens": threshold}
                    )
                    variants.append(
                        (
                            name,
                            ContextManager(candidate_config)
                            .prepare(replay_history, tools)
                            .messages,
                        )
                    )
                accepted = []
                for name, candidate in variants:
                    reasons = preserved(messages, candidate, raw_results)
                    size = len(encoded(wire(candidate, tools, adapter)))
                    byte_bound = adapter.count_input_tokens(candidate, tools)
                    candidate_record = {
                        "name": name,
                        "wire_bytes": size,
                        "estimated_tokens": ContextManager.estimate_tokens(candidate, tools),
                        "byte_reservation": byte_bound,
                        "observed_usage": None,
                        "evidence_failures": reasons,
                        "eligible": not reasons and size < row["wire_bytes"],
                        "would_fit_recorded_remaining_input": row["cumulative_input_before"]
                        + byte_bound
                        <= 60000,
                        "interpretation": "fixed_action_projection_not_autonomous_repair",
                    }
                    if candidate_record["eligible"]:
                        accepted.append(name)
                    row["candidates"].append(candidate_record)
                if "line_pages" in accepted and "remaining_budget_context" in accepted:
                    combined = (
                        ContextManager(
                            context.model_copy(
                                update={
                                    "compaction_trigger_tokens": min(
                                        6000,
                                        max(2000, (60000 - row["cumulative_input_before"]) // 4),
                                    )
                                }
                            )
                        )
                        .prepare(paged, tools)
                        .messages
                    )
                    row["candidates"].append(
                        {
                            "name": "combined",
                            "wire_bytes": len(encoded(wire(combined, tools, adapter))),
                            "observed_usage": None,
                            "evidence_failures": preserved(messages, combined, raw_results),
                        }
                    )
            requests.append(row)
            current_request = row
        elif kind == "model_responded" and current_request:
            ledger_cursor += 1
            current_request["response_observed"] = True
            if current_request["observed_usage"] != payload.get("usage"):
                errors.append(f"step_{step}_usage_mismatch")
        elif kind == "error" and current_request:
            current_request["error"] = payload.get("error")
    for row in requests:
        if not row["hash_verified"]:
            error = row.get("error", {})
            row["transmission"] = (
                "confirmed_local_refusal"
                if not row.get("response_observed")
                and error.get("message") == "campaign pre-request input reservation exceeded"
                and row["cumulative_input_before"] + row["byte_reservation"] > 60000
                else "unverifiable"
            )
            if row["transmission"] == "unverifiable":
                errors.append(f"step_{row['step']}_request_hash_unmatched")
        else:
            row["transmission"] = "ledger_matched"
    missing = set(calls) - all_seen_results
    if missing:
        errors.append("missing_tool_results")
    if not requests:
        errors.append("missing_request_views")
    if any(row["hash_verified"] and not row.get("response_observed") for row in requests):
        errors.append("completed_ledger_without_response_event")
    if errors:
        for row in requests:
            row["candidates"] = []
            row["candidate_skip_reason"] = "baseline_incomplete"
    return {
        "stage": stage,
        "implementation_commit": record.get("implementation_commit"),
        "config_sha256": file_sha(config_path),
        "trajectory_sha256": file_sha(events_path),
        "result_sha256": file_sha(run / "result.json"),
        "status": final["status"],
        "stop_reason": final.get("stop_reason"),
        "stop_reproduced": (
            bool(requests)
            and final.get("stop_reason") == "agent_completed"
            and requests[-1].get("response_observed", False)
            or bool(requests)
            and usage_complete
            and final.get("stop_reason") == "token_budget_exceeded"
            and (
                used_input > config["agent_config"]["max_input_tokens"]
                or requests[-1]["transmission"] == "confirmed_local_refusal"
            )
        ),
        "errors": sorted(set(errors)),
        "requests": requests,
        "reads": reads,
        "matched_request_hashes": mapped,
        "transient_anchor_source": "recorded_request_anchor; validate against tool provenance",
    }
