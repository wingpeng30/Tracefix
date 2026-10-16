import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from tracefix.mcp_serena import SerenaMCP
from tracefix.messages import ToolCall
from tracefix.repository_rules.access import ToolAccessPlanner
from tracefix.repository_rules.catalog import RepositoryRules
from tracefix.repository_rules.delivery import RuleDeliveryLedger
from tracefix.repository_rules.gate import RuleAccessGate
from tracefix.repository_rules.mcp_output import McpSourceGate
from tracefix.repository_rules.mcp_snapshot import SnapshotMcpRuleGuard
from tracefix.repository_rules.source import RuleSourceView


def assemble(tmp_path, monkeypatch, response):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "widget.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("Widget rules 🙂", encoding="utf-8")
    for args in (("init", "-q"), ("add", "."),
                 ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-qm", "fixture")):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    rules, ledger = RepositoryRules(RuleSourceView(root)), RuleDeliveryLedger()
    adapter = SnapshotMcpRuleGuard(McpSourceGate(rules, ledger))
    manager = SerenaMCP(root, tmp_path, "sha256:" + "a" * 64)
    manager.bind_source_result_guard(adapter)
    calls = []

    async def fake(name, snapshot, method, arguments):
        calls.append((method, snapshot))
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text=response)])

    monkeypatch.setattr(manager, "_call_async", fake)
    monkeypatch.setattr(manager, "_remove_container",
                        lambda name: manager._active.discard(name) or True)
    tool = manager.tools()[1]
    return manager, tool, adapter, rules, ledger, calls


def symbol():
    return json.dumps([{"relative_path": "widget.py", "name_path": "f", "kind": "Function",
                        "body_location": {"start_line": 0, "end_line": 1},
                        "body": "def f():\n    return 1"}])


def call(number=1):
    return ToolCall(id=f"mcp-runtime-{number}", name="mcp_serena_find_symbol",
                    arguments={"name_path_pattern": "f", "include_body": True})


def test_guarded_manager_withholds_then_delivers_verified_snapshot(tmp_path, monkeypatch):
    manager, tool, adapter, rules, ledger, calls = assemble(tmp_path, monkeypatch, symbol())
    gate = RuleAccessGate(ToolAccessPlanner(), rules, ledger,
                          source_validators={tool.spec.name: adapter})
    assert gate.check(call()).allowed
    pending = tool.execute(call())
    assert pending.success and pending.output["rules_pending"]
    assert pending.output["text"] == ""
    assert "return 1" not in pending.model_dump_json()
    assert pending.output["source_refs"][0]["path"] == "widget.py"
    messages = gate.close_batch(())
    gate.confirm_response(gate.prepare_request(messages))
    result = tool.execute(call(2))
    assert result.success and not result.output["rules_pending"]
    assert result.output["text"] == symbol()
    assert result.output["source_sha256"] == pending.output["source_sha256"]
    assert len(calls) == 2 and not manager._active
    assert all(not snapshot.exists() for _, snapshot in calls)


@pytest.mark.parametrize("response", [
    "Shortened result:\nUNVERIFIED_SECRET",
    RuntimeError("UNVERIFIED_SECRET from transport"),
    '[{"relative_path":"outside.py","body":"UNVERIFIED_SECRET"}]',
], ids=["shortened", "transport", "outside"])
def test_unverifiable_response_and_transport_detail_never_escape(tmp_path, monkeypatch, response):
    manager, tool, adapter, rules, ledger, calls = assemble(tmp_path, monkeypatch, response)
    result = tool.execute(call())
    assert not result.success and "UNVERIFIED_SECRET" not in result.model_dump_json()
    assert not manager._active and all(not snapshot.exists() for _, snapshot in calls)


def test_service_error_detail_is_withheld_and_oversize_json_is_not_partially_returned(
    tmp_path, monkeypatch,
):
    manager, tool, adapter, rules, ledger, calls = assemble(tmp_path, monkeypatch, symbol())

    async def failure(*args):
        return SimpleNamespace(isError=True, content=[
            SimpleNamespace(type="text", text="UNVERIFIED_SECRET")])

    monkeypatch.setattr(manager, "_call_async", failure)
    error = tool.execute(call())
    assert not error.success and "UNVERIFIED_SECRET" not in error.error

    async def long(*args):
        return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text=symbol())])

    monkeypatch.setattr(manager, "_call_async", long)
    manager.max_output_chars = 20
    oversized = tool.execute(call(2))
    assert not oversized.success and "verification limit" in oversized.error
    assert "return 1" not in oversized.model_dump_json()


def test_guarded_direct_method_rejects_control_before_source_export(tmp_path, monkeypatch):
    manager, *_ = assemble(tmp_path, monkeypatch, symbol())
    monkeypatch.setattr(manager, "_snapshot", lambda path: pytest.fail("unknown method exported"))
    with pytest.raises(RuntimeError, match="not permitted"):
        manager.call("execute_shell_command", {"command": "anything"}, "unknown")


def test_mcp_validator_is_explicit_and_cannot_be_used_to_exempt_source_calls(tmp_path, monkeypatch):
    manager, tool, adapter, rules, ledger, calls = assemble(tmp_path, monkeypatch, symbol())
    gate = RuleAccessGate(ToolAccessPlanner(), rules, ledger,
                          source_validators={tool.spec.name: adapter})
    for args in ({"name_path_pattern": "f", "relative_path": ".env"},
                 {"name_path_pattern": "f", "origin": "host"}):
        with pytest.raises(ValueError):
            gate.check(ToolCall(id="bad", name=tool.spec.name, arguments=args))
    with pytest.raises(ValueError, match="not_supported"):
        RuleAccessGate(ToolAccessPlanner(), rules, ledger,
                       source_validators={"read_file": adapter})
    with pytest.raises(ValueError, match="cannot_bypass"):
        RuleAccessGate(ToolAccessPlanner(), rules, ledger,
                       non_source_tools=frozenset({tool.spec.name}))


@pytest.mark.parametrize(("index", "expected"), [
    (0, "462811b3c6f59ea8e78c1fc0cf76c7b5234ec475386f3f7da0ffcf6be6eac6fc"),
    (1, "5e944c87a8c7ded4e69900aabfa944404c28c8a8cf329ebf8004134ab77e170f"),
    (2, "556991793519d8767999f3e1e891b46f3ac265e9e2258663bbe4493886e5d444"),
])
def test_original_serena_tool_specs_match_accepted_g5(tmp_path, index, expected):
    manager = SerenaMCP(tmp_path, tmp_path, "sha256:" + "a" * 64)
    spec = manager.tools()[index].spec.model_dump(mode="json")
    raw = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(raw).hexdigest() == expected
