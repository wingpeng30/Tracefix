from types import SimpleNamespace

import pytest

from tracefix.messages import ToolCall
from tracefix.repository_rules.access import ToolAccessPlanner
from tracefix.repository_rules.domain import RuleSelection
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.protocol import RemoteRulePorts, execute_rule_port
from tracefix.tools.base import ToolResult


def ports():
    calls = []
    selector = SimpleNamespace(select=lambda path: RuleSelection.create(path, (), ()))
    metadata = SimpleNamespace(resolve=lambda plan: ("a.py",))
    source = SimpleNamespace(read_bytes=lambda path: (
        "needle🙂".encode() if path == "a.py" else None))

    class Channel:
        def call(self, name, arguments):
            calls.append((name, arguments))
            output = execute_rule_port(arguments, selector=selector, metadata=metadata,
                                       source=source)
            return ToolResult(call_id="private", tool_name=name, success=True, output=output)

    authorized = []
    return RemoteRulePorts(Channel(), lambda: authorized.append(True)), calls, authorized


def test_roundtrip_versioned_ports_have_one_authorization_per_call():
    remote, calls, authorized = ports()
    assert remote.select("a.py").target_path == "a.py"
    plan = ToolAccessPlanner().plan(ToolCall(id="x", name="search_code", arguments={"query": "n"}))
    assert remote.resolve(plan) == ("a.py",)
    assert remote.read_bytes("a.py") == "needle🙂".encode()
    assert remote.read_bytes("missing.py") is None
    assert len(authorized) == len(calls) == 4
    assert all(name == "__repository_rules__" for name, _ in calls)


@pytest.mark.parametrize("arguments", [
    {"version": True, "operation": "read", "target": "a.py"},
    {"version": 2, "operation": "read", "target": "a.py"},
    {"version": 1, "operation": "read", "target": "../a.py"},
    {"version": 1, "operation": "read", "target": ".git/config"},
    {"version": 1, "operation": "read"},
    {"version": 1, "operation": "paths", "target": "a.py"},
    {"version": 1, "operation": "read", "target": "a.py", "origin": "host_lifecycle"},
])
def test_bad_requests_do_not_touch_any_port(arguments):
    def fail(*args):
        pytest.fail("invalid request touched a source port")

    with pytest.raises(RuleError):
        execute_rule_port(arguments, selector=SimpleNamespace(select=fail),
                          metadata=SimpleNamespace(resolve=fail),
                          source=SimpleNamespace(read_bytes=fail))


@pytest.mark.parametrize("output", [None, {}, {"version": 2, "operation": "read"},
    {"version": True, "operation": "read", "content_base64": None},
    {"version": 1, "operation": "read"},
    {"version": 1, "operation": "read", "content_base64": "***"},
    {"version": 1, "operation": "read", "content_base64": "a" * 1_333_337},
])
def test_malformed_response_is_not_a_missing_source(output):
    channel = SimpleNamespace(call=lambda *args: ToolResult(
        call_id="x", tool_name="__repository_rules__", success=True, output=output))
    with pytest.raises(RuleError):
        RemoteRulePorts(channel, lambda: None).read_bytes("a.py")


def test_denied_host_control_never_calls_channel():
    def deny():
        raise RuleError("denied")

    def fail(*args):
        pytest.fail("denied control dispatched")

    with pytest.raises(RuleError, match="denied"):
        RemoteRulePorts(SimpleNamespace(call=fail), deny).read_bytes("a.py")
