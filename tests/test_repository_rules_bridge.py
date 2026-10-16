import io
import json
import sys
from types import SimpleNamespace

import pytest

from tracefix.agent_bridge import serve_requests
from tracefix.capabilities import (
    CallerOrigin,
    CapabilityCatalog,
    CapabilityPolicy,
    ExecutionProfile,
    ToolDispatcher,
    executor_policy_document,
)
from tracefix.docker_backend import _BridgeSession
from tracefix.exceptions import WorkspaceError
from tracefix.messages import ToolCall
from tracefix.repository_rules.domain import RuleSelection
from tracefix.tools.base import ToolRegistry


def policy(enabled=True):
    names = ("repository_rules_read",) if enabled else ()
    return CapabilityPolicy(CapabilityCatalog.for_tool_names(names), ExecutionProfile.INSPECT)


def test_control_is_not_model_authority_and_does_not_change_disabled_identity():
    enabled, disabled = policy(), policy(False)
    assert enabled.authorize("repository_rules_read", CallerOrigin.HOST_LIFECYCLE).allowed
    for origin in (CallerOrigin.MODEL, CallerOrigin.RECOVERY, CallerOrigin.VALIDATION_GATE,
                   CallerOrigin.JOB_COORDINATOR, CallerOrigin.INDEPENDENT_VALIDATION):
        assert not enabled.authorize("repository_rules_read", origin).allowed
    assert not disabled.authorize("repository_rules_read", CallerOrigin.HOST_LIFECYCLE).allowed
    old = executor_policy_document("inspect", artifacts_enabled=False, skills_enabled=False)
    new = executor_policy_document("inspect", artifacts_enabled=False, skills_enabled=False,
                                   repository_rules_enabled=True)
    assert "repository_rules_protocol_version" not in old
    assert new["repository_rules_protocol_version"] == 1
    assert old["capability_policy_sha256"] != new["capability_policy_sha256"]


@pytest.mark.parametrize("enabled", [False, True])
def test_only_enabled_and_authorized_bridge_control_touches_ports(tmp_path, monkeypatch, enabled):
    calls = []

    def select(path):
        calls.append(path)
        return RuleSelection.create(path, (), ())

    ports = SimpleNamespace(selector=SimpleNamespace(select=select), metadata=None, source=None)
    call = ToolCall(id="host-control", name="__repository_rules__", arguments={
        "version": 1, "operation": "select", "target": "a.py"})
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(call.model_dump_json() + "\n"))
    monkeypatch.setattr(sys, "stdout", stream)
    serve_requests(ToolRegistry(), ToolDispatcher(policy(enabled)), set(), tmp_path,
                   rule_ports=ports if enabled else None)
    response = json.loads(stream.getvalue())["result"]
    assert response["success"] is enabled
    assert calls == (["a.py"] if enabled else [])
    assert response["call_id"] == call.id


@pytest.mark.parametrize(("enabled", "version", "accepted"), [
    (False, None, True), (True, 1, True), (True, None, False),
    (True, True, False), (True, 2, False), (False, 1, False),
])
def test_real_process_bridge_negotiates_rule_mode_before_use(tmp_path, enabled, version, accepted):
    actual = policy(enabled)
    frame = {"type": "hello", "protocol": 1, "run_id": "fixture", "tools": [],
             "execution_profile": "inspect", "capability_catalog_sha256": actual.catalog.sha256,
             "capability_policy_sha256": actual.sha256}
    if version is not None:
        frame["repository_rules_protocol_version"] = version
    code = ("import sys; print(" + repr(json.dumps(frame)) + ", flush=True); "
            "sys.stdin.read()")

    def start():
        return _BridgeSession([sys.executable, "-u", "-c", code], 2, tmp_path / "journal.jsonl",
                               execution_profile=ExecutionProfile.INSPECT,
                               repository_rules_enabled=enabled)

    if accepted:
        session = start()
        try:
            assert session.repository_rules_enabled is enabled
            assert session.capability_policy.sha256 == actual.sha256
        finally:
            session.close(force=True)
    else:
        with pytest.raises(WorkspaceError, match="rules protocol negotiation"):
            start()
