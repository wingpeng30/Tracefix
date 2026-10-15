"""The bridge assembly must enforce both host tickets and execution-side policy."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tracefix.agent_bridge import _job_controller
from tracefix.artifacts import ArtifactStore
from tracefix.capabilities import CallerOrigin, CapabilityCatalog, CapabilityPolicy
from tracefix.jobs import docker_context, process_tree
from tracefix.jobs.constants import JOB_CONTROL_CAPABILITIES
from tracefix.jobs.domain import JobError
from tracefix.jobs.executions import TestExecution as ExecutionRow
from tracefix.jobs.protocol import WorkerStartRequest
from tracefix.messages import ToolCall
from tracefix.tools import create_default_tool_registry


@pytest.mark.parametrize("profile,clean", [("workspace_write", True), ("inspect", True),
                                          ("workspace_write", False)])
def test_bridge_refuses_unsafe_preflight_or_policy_before_any_test_launch(
    tmp_path, monkeypatch, profile, clean,
):
    controls = frozenset({(1, "10"), (2, "20"), (3, "30")})
    probes = []

    class Scope:
        def __init__(self, selected):
            assert selected == controls
            probes.append(True)

        def close(self):
            return clean

    monkeypatch.setattr(docker_context, "bind_controller_tokens", lambda fixed: controls)
    monkeypatch.setattr(process_tree, "DockerProcessScope", Scope)
    workspace, evidence = tmp_path / "workspace", tmp_path / "evidence"
    workspace.mkdir()
    evidence.mkdir()
    artifacts = ArtifactStore(evidence / "artifacts", run_id="root-run", streaming_enabled=True)
    tools = create_default_tool_registry(workspace, evidence_dir=evidence)
    policy = CapabilityPolicy(CapabilityCatalog.for_tool_names(
        (*tools.names, *JOB_CONTROL_CAPABILITIES)), profile)
    context = SimpleNamespace(
        fixed_control_tokens=((1, "10"), (2, "20")), logical_run_id="root-run",
        worker_epoch="c" * 32, host_owner_epoch="b" * 32,
        session_deadline=datetime.now(UTC) + timedelta(seconds=30))
    if not clean:
        with pytest.raises(JobError, match="scope_preflight_failed"):
            _job_controller(context, tools, artifacts, evidence, workspace, policy)
        assert probes == [True] and artifacts.quota().storage_bytes == 0
        return
    controller, dispatcher = _job_controller(
        context, tools, artifacts, evidence, workspace, policy)
    call = ToolCall(id="untrusted-command", name="run_tests",
                    arguments={"command": "echo arbitrary-shell"})
    ticket = ExecutionRow(execution_id="a" * 32, call_id=call.id, owner_epoch="b" * 32,
        invocation_sha256=hashlib.sha256(call.model_dump_json().encode()).hexdigest(),
        phase="dispatched", process_started=None)
    request = WorkerStartRequest(version=1, worker_epoch="c" * 32, logical_run_id="root-run",
        job_id="job_" + "d" * 32, ticket=ticket, call_json=call.model_dump_json(),
        deadline=context.session_deadline)
    assert not dispatcher.authorize("job_start_control", CallerOrigin.MODEL).allowed
    if profile == "inspect":
        with pytest.raises(JobError):
            controller.start(request)
        with pytest.raises(JobError, match="not_available"):
            controller.tickets.get(ticket.execution_id)
    else:
        receipt = controller.start(request)
        assert receipt.execution.phase == "not_started"
        assert receipt.execution.process_started is False and receipt.job is None
    assert probes == [True] and artifacts.quota().storage_bytes == 0
    assert controller.shutdown(worker_epoch="c" * 32)["closed"]
