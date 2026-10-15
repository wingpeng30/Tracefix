"""Preparation freezes arguments; dispatch cannot be replayed after uncertainty."""

import pytest

from tracefix.messages import ToolCall
from tracefix.tools.base import ToolSpec
from tracefix.tools.invocation import PreparedInvocation


class ProcessTool:
    spec = ToolSpec(name="run_tests", description="fixture")

    def __init__(self, *, fail=False, spawn=True):
        self.on_process_started = None
        self.calls = []
        self.fail = fail
        self.spawn = spawn

    def prepare(self, call):
        self.calls.append(("prepare", call.arguments.copy()))
        call.arguments["command"] = "mutated by prepare"

    def execute(self, call):
        self.calls.append(("execute", call.arguments.copy()))
        if self.spawn:
            self.on_process_started()
            self.on_process_started()
        if self.fail:
            raise OSError("dispatched result unknown")
        return call.arguments


@pytest.mark.parametrize("fail", [False, True])
def test_arguments_are_frozen_callback_is_scoped_and_dispatched_calls_never_replay(fail):
    tool = ProcessTool(fail=fail)
    def old_callback():
        pass
    tool.on_process_started = old_callback
    call = ToolCall(id="one", name="run_tests", arguments={"command": "pytest -q tests"})
    invocation = PreparedInvocation(tool, call)
    call.arguments["command"] = "changed after admission"
    starts = []
    if fail:
        with pytest.raises(OSError, match="unknown"):
            invocation.execute(lambda: starts.append(True))
    else:
        assert invocation.execute(lambda: starts.append(True)) == {"command": "pytest -q tests"}
    assert tool.calls[-1] == ("execute", {"command": "pytest -q tests"})
    assert starts == [True]
    assert tool.on_process_started is old_callback
    with pytest.raises(RuntimeError, match="already been dispatched"):
        invocation.execute()
    assert len(tool.calls) == 2


def test_no_process_start_does_not_invent_budget_use():
    tool = ProcessTool(spawn=False)
    starts = []
    PreparedInvocation(tool, ToolCall(id="one", name="run_tests")).execute(
        lambda: starts.append(True)
    )
    assert starts == []


def test_legacy_tool_keeps_execute_contract_without_callback_or_prepare_wrapper():
    class Legacy:
        spec = ToolSpec(name="read_file", description="fixture")

        def execute(self, call):
            return call.id

    tool = Legacy()
    invocation = PreparedInvocation(tool, ToolCall(id="legacy", name="read_file"))
    assert not invocation.tracks_process_start
    assert invocation.execute() == "legacy"
    assert not hasattr(tool, "on_process_started")
    with pytest.raises(ValueError, match="differs"):
        PreparedInvocation(tool, ToolCall(id="bad", name="run_tests"))
