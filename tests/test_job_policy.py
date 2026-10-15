from types import SimpleNamespace

import pytest

from tracefix.capabilities import (
    CallerOrigin,
    CapabilityCatalog,
    CapabilityError,
    CapabilityPolicy,
    ToolDispatcher,
)
from tracefix.jobs.policy import JobActivityGuard
from tracefix.messages import ToolCall


def test_active_job_blocks_prepare_mutation_validation_and_safe_checkpoint():
    state = SimpleNamespace(active=True, has_unknown=False)
    names = ("run_tests", "apply_patch", "read_file", "checkpoint_commit", "start_test_job")
    policy = CapabilityPolicy(CapabilityCatalog.for_tool_names(names), "workspace_write")
    dispatcher = ToolDispatcher(policy, execution_guard=JobActivityGuard(state))
    prepared = []
    tool = SimpleNamespace(prepare=lambda call: prepared.append(call))
    with pytest.raises(CapabilityError, match="active_test"):
        dispatcher.prepare_invocation(tool, ToolCall(id="r", name="run_tests"), CallerOrigin.MODEL)
    assert prepared == []
    assert not dispatcher.authorize("apply_patch", CallerOrigin.MODEL).allowed
    assert not dispatcher.authorize("run_tests", CallerOrigin.VALIDATION_GATE).allowed
    assert not dispatcher.authorize("checkpoint_commit", CallerOrigin.HOST_LIFECYCLE).allowed
    assert dispatcher.authorize("read_file", CallerOrigin.MODEL).allowed
    state.active = False
    assert dispatcher.authorize("run_tests", CallerOrigin.MODEL).allowed


def test_unknown_execution_blocks_new_side_effects_but_allows_historical_inspection():
    state = SimpleNamespace(active=False, has_unknown=True)
    names = ("run_tests", "read_job_output", "get_job_status", "apply_patch", "cancel_job")
    policy = CapabilityPolicy(CapabilityCatalog.for_tool_names(names), "workspace_write")
    dispatcher = ToolDispatcher(policy, execution_guard=JobActivityGuard(state))
    assert not dispatcher.authorize("run_tests", CallerOrigin.MODEL).allowed
    assert not dispatcher.authorize("apply_patch", CallerOrigin.MODEL).allowed
    assert dispatcher.authorize("read_job_output", CallerOrigin.MODEL).allowed
    assert dispatcher.authorize("get_job_status", CallerOrigin.MODEL).allowed
    assert dispatcher.authorize("cancel_job", CallerOrigin.MODEL).allowed
