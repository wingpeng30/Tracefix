"""Synthetic evidence, never private campaign records or provider requests."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tracefix.context import ContextConfig, ContextManager
from tracefix.messages import Message, ToolCall
from tracefix.tools.base import ToolResult

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


h = module("history_analysis")
a = module("analyze_live_history")
m = module("history_micro")


def campaign(tmp_path):
    root = tmp_path / "campaign"
    root.mkdir()
    config = {
        "model_name": "deepseek/deepseek-flash",
        "llm_timeout_seconds": 60,
        "llm_max_retries": 0,
        "per_request_output_tokens": 2048,
        "agent_config": {
            "context": ContextConfig().model_dump(mode="json"),
            "max_input_tokens": 60000,
        },
    }
    messages = [
        Message(role="system", content="Trusted instructions"),
        Message(role="user", content="Fix widget; preserve tests"),
    ]
    adapter = h.adapter_for(config)
    view = ContextManager(ContextConfig()).prepare(messages, ())
    sha = h.digest(h.encoded(h.wire(view.messages, (), adapter)))
    usage = {"input_tokens": 200, "output_tokens": 10, "total_tokens": 210}
    entry = {
        "request_sha256": sha,
        "status": "completed",
        "usage": usage,
        "input_bound": adapter.count_input_tokens(view.messages, ()),
    }
    records = []
    for stage in ("one", "two"):
        directory = root / stage
        directory.mkdir()
        cfg = directory / "config.json"
        cfg.write_text(json.dumps(config), encoding="utf-8")
        (directory / "result.json").write_text(
            json.dumps(
                {
                    "status": "completed",
                    "stop_reason": "agent_completed",
                    "source_commit": "synthetic",
                }
            ),
            encoding="utf-8",
        )
        events = [
            {
                "event_type": "message_added",
                "step": 0,
                "payload": {"message": msg.model_dump(mode="json")},
            }
            for msg in messages
        ]
        events += [
            {
                "event_type": "model_request_view",
                "step": 1,
                "payload": {
                    "messages": [msg.model_dump(mode="json") for msg in view.messages],
                    "tools": [],
                    "context": {"estimated_tokens_after": view.estimated_tokens_after},
                },
            },
            {"event_type": "model_responded", "step": 1, "payload": {"usage": usage}},
        ]
        (directory / "trajectory.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events), encoding="utf-8"
        )
        records.append(
            {
                "stage": stage,
                "config_sha256": h.file_sha(cfg),
                "result_path": str(directory / "result.json"),
            }
        )
    (root / "runs.json").write_text(json.dumps({"runs": records}), encoding="utf-8")
    (root / "requests.json").write_text(json.dumps({"requests": [entry, entry]}), encoding="utf-8")
    return root, records, entry


def test_pair_identical_hashes_chronologically(tmp_path):
    root, _, _ = campaign(tmp_path)
    result = a.analyze(root, tmp_path / "analysis")
    assert result["matched_requests"] == 2
    assert result["ledger_identity_complete"] and result["inputs_unchanged"]
    assert all(not r["errors"] and r["stop_reproduced"] for r in result["runs"])
    assert result["supplier_request_attempts"] == 0
    with pytest.raises(ValueError, match="already exists"):
        a.analyze(root, tmp_path / "analysis")
    with pytest.raises(ValueError, match="outside"):
        a.analyze(root, root / "nested")


@pytest.mark.parametrize(
    "damage",
    ["hash", "usage", "partial", "negative", "pending", "config", "malformed", "response", "views"],
)
def test_incomplete_evidence_excludes_candidates(tmp_path, damage):
    root, records, entry = campaign(tmp_path)
    record = records[0]
    path = root / "one" / "trajectory.jsonl"
    if damage == "hash":
        entry["request_sha256"] = "wrong"
    elif damage in ("usage", "partial", "negative"):
        entry["usage"] = (
            {"input_tokens": -1}
            if damage == "negative"
            else ({"input_tokens": 200} if damage == "partial" else None)
        )
    elif damage == "pending":
        entry["status"] = "pending"
    elif damage == "config":
        record["config_sha256"] = "wrong"
    elif damage == "malformed":
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n{broken")
    else:
        events = [json.loads(line) for line in path.read_text().splitlines()]
        removed = "model_responded" if damage == "response" else "model_request_view"
        path.write_text("\n".join(json.dumps(e) for e in events if e["event_type"] != removed))
    result = h.analyze_run(record, root, [entry])
    assert result["errors"]
    assert all(not r["candidates"] for r in result["requests"])
    if damage in ("usage", "partial", "negative", "pending"):
        assert result["requests"][0]["cumulative_input_after"] is None


@pytest.mark.parametrize(
    "raw,returned,current,ever,expected",
    [
        (set(), set(), set(), set(), "cannot_judge"),
        ({1}, set(), set(), set(), "new_range"),
        ({1}, {1}, {1}, {1}, "fully_previously_visible"),
        ({1, 2}, {1}, {1}, {1}, "partial_overlap"),
        ({1}, {1}, set(), set(), "necessary_reread_of_previously_hidden_lines"),
        ({1}, {1}, set(), {1}, "necessary_reread_after_context_eviction"),
    ],
)
def test_read_classification(raw, returned, current, ever, expected):
    assert h.classify_read(raw, returned, current, ever)[0] == expected


def test_line_pages_explicit_gaps_and_exact_content():
    raw = {i: "source content " * 5 for i in range(1, 101)}
    result = ToolResult(
        call_id="read",
        tool_name="read_file",
        success=True,
        output={
            "content": "\n".join(f"{i:>6} | {value}" for i, value in raw.items()),
            "start_line": 1,
            "end_line": 100,
            "total_lines": 120,
        },
    )
    page = h.line_page(result, limit=400)
    selected = h.lines(page["output"]["content"])
    assert selected and all(raw[n] == value for n, value in selected.items())
    assert page["output"]["omitted_ranges"]
    assert page["output"]["next_start_line"] == min(set(raw) - set(selected))
    assert h.ranges([3, 1, 2, 7]) == [[1, 3], [7, 7]]
    msg = Message(role="tool", tool_call_id="read", content=json.dumps(page))
    assert h.visible_lines(msg, raw) == set(selected)
    assert h.visible_lines(msg.model_copy(update={"content": "pruned"}), raw) is None


def test_pairing_and_mandatory_evidence():
    system = Message(role="system", content="rules")
    task = Message(role="user", content="contract")
    calls = [ToolCall(id="test", name="run_tests"), ToolCall(id="patch", name="apply_patch")]
    request = Message(role="assistant", tool_calls=tuple(calls))
    results = {
        call.id: ToolResult(call_id=call.id, tool_name=call.name, success=True, output={})
        for call in calls
    }
    messages = [system, task, request] + [
        Message(role="tool", tool_call_id=call.id, content=results[call.id].model_dump_json())
        for call in calls
    ]
    assert h.tool_pairing(messages)
    assert not h.preserved(messages, messages, results)
    reasons = h.preserved(messages, messages[:3], results)
    assert "invalid_tool_pairing" in reasons
    assert "lost_latest_test_result" in reasons and "lost_latest_patch_result" in reasons
    assert not h.tool_pairing(messages + [messages[-1]])


@pytest.mark.parametrize("damage", ["missing", "duplicate", "orphan", "unexecuted"])
def test_tool_event_pairing(tmp_path, damage):
    root, records, entry = campaign(tmp_path)
    call = ToolCall(id="r", name="read_file", arguments={"path": "widget.py"})
    result = ToolResult(
        call_id="r",
        tool_name="read_file",
        success=True,
        output={"path": "widget.py", "content": "1 | x = 1"},
    )
    added = []
    if damage != "orphan":
        added.append(
            {
                "event_type": "message_added",
                "step": 2,
                "payload": {
                    "message": Message(role="assistant", tool_calls=(call,)).model_dump(mode="json")
                },
            }
        )
    if damage not in ("orphan", "unexecuted"):
        added.append(
            {
                "event_type": "tool_called",
                "step": 2,
                "payload": {"call": call.model_dump(mode="json")},
            }
        )
    if damage != "missing":
        returned = {
            "event_type": "tool_returned",
            "step": 2,
            "payload": {"result": result.model_dump(mode="json")},
        }
        added += [returned] * (2 if damage == "duplicate" else 1)
    path = root / "one" / "trajectory.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n" + "\n".join(json.dumps(e) for e in added))
    finding = h.analyze_run(records[0], root, [entry])
    assert finding["errors"]
    assert all(not row["candidates"] for row in finding["requests"])


def test_supplier_guard_blocks_import_and_transport():
    import importlib
    import socket

    from tracefix.regression_replay import forbid_live_access

    with forbid_live_access():
        with pytest.raises(AssertionError):
            importlib.import_module("litellm")
        with pytest.raises(AssertionError):
            socket.create_connection(("example.invalid", 443))


@pytest.mark.parametrize("variant", ["line_pages", "context"])
def test_real_tools_feedback_and_cache_invalidation(tmp_path, variant):
    result = m.run_micro(tmp_path / f"space path {variant}", Path(sys.executable), variant)
    assert result["independent_passed"] and result["source_unchanged"]
    assert result["feedback_observed"] and result["fresh_read_observed"]
    assert result["clipping_observed"] and result["test_runs"] == 6
    assert result["supplier_requests"] == 0
    if variant == "context":
        assert result["compactions"] > 0
