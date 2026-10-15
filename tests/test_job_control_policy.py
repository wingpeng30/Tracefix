from __future__ import annotations

from types import SimpleNamespace

import pytest

from tracefix.capabilities import (
    CallerOrigin,
    CapabilityCatalog,
    CapabilityError,
    CapabilityPolicy,
    ToolDispatcher,
)
from tracefix.jobs.constants import JOB_CONTROL_CAPABILITIES
from tracefix.jobs.policy import JobActivityGuard
from tracefix.messages import ToolCall


def policy(profile="workspace_write"):
    return CapabilityPolicy(CapabilityCatalog.for_tool_names((
        "run_tests", "read_file", "apply_patch", "source_snapshot_export",
        *JOB_CONTROL_CAPABILITIES)), profile)


def test_disabled_job_control_keeps_legacy_policy_identity_and_grants_no_new_origin():
    legacy = CapabilityPolicy(CapabilityCatalog.for_tool_names(("read_file", "run_tests")),
                               "workspace_write")
    assert legacy.VERSION == "tracefix-capability-policy-v3"
    # Frozen from the 6b70e6c policy, not recomputed from the new implementation.
    assert legacy.sha256 == "309b682c74d8057cd54d1c13f9fa9619e28dc2e7d480918c1d9588071bae95fa"
    assert not legacy.authorize("run_tests", CallerOrigin.JOB_COORDINATOR).allowed
    assert legacy.authorize("run_tests", CallerOrigin.MODEL).allowed


def test_private_control_requires_exact_trusted_origin_and_a_complete_versioned_catalog():
    current = policy()
    assert current.VERSION == "tracefix-capability-policy-v4-jobs"
    for name in JOB_CONTROL_CAPABILITIES:
        assert not current.catalog.get(name).model_callable
        for origin in CallerOrigin:
            assert current.authorize(name, origin).allowed is (
                origin is CallerOrigin.JOB_COORDINATOR)
    assert current.authorize("run_tests", CallerOrigin.JOB_COORDINATOR).allowed
    for name in ("read_file", "apply_patch", "source_snapshot_export", "__job_start__"):
        assert not current.authorize(name, CallerOrigin.JOB_COORDINATOR).allowed
    with pytest.raises(CapabilityError, match="incomplete"):
        CapabilityPolicy(CapabilityCatalog.for_tool_names(("job_start_control",)),
                         "workspace_write")


def test_inspect_never_starts_a_process_through_a_private_control():
    current = policy("inspect")
    for name in ("run_tests", "job_start_control"):
        assert current.authorize(name, CallerOrigin.JOB_COORDINATOR).reason_code == "profile_denied"
    for name in JOB_CONTROL_CAPABILITIES[1:]:
        assert current.authorize(name, CallerOrigin.JOB_COORDINATOR).allowed


def test_active_or_unknown_work_still_allows_cleanup_but_not_another_controlled_start():
    state = SimpleNamespace(active=True, has_unknown=False)
    dispatcher = ToolDispatcher(policy(), execution_guard=JobActivityGuard(state))
    prepared = []
    tool = SimpleNamespace(prepare=lambda call: prepared.append(call))
    with pytest.raises(CapabilityError, match="active_test"):
        dispatcher.prepare_invocation(tool, ToolCall(id="second", name="job_start_control"),
                                       CallerOrigin.JOB_COORDINATOR)
    assert prepared == []
    for unknown in (False, True):
        state.has_unknown = unknown
        for name in ("job_status_control", "job_read_control",
                     "job_cancel_control", "job_shutdown_control"):
            assert dispatcher.authorize(name, CallerOrigin.JOB_COORDINATOR).allowed
        assert not dispatcher.authorize("job_start_control", CallerOrigin.JOB_COORDINATOR).allowed
        assert not dispatcher.authorize("job_shutdown_control", CallerOrigin.MODEL).allowed
