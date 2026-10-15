from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tracefix.capabilities import CallerOrigin
from tracefix.jobs.constants import JOB_CONTROL_CAPABILITIES
from tracefix.jobs.domain import JobError
from tracefix.jobs.ports import JobSourceState
from tracefix.jobs.remote_runtime import RemoteJobRuntime
from tracefix.jobs.test_budget import TestExecutionLedger
from tracefix.messages import ToolCall
from tracefix.tools.base import BaseTool, ToolRegistry, ToolResult, ToolSpec
from tracefix.tools.jobs import JOB_TOOL_NAMES, register_job_tools


class PreparedRemote(BaseTool):
    spec = ToolSpec(name="run_tests", description="fixed existing pytest schema")

    def prepare(self, call):
        raise AssertionError("denied calls must never prepare")

    def execute(self, call):
        raise AssertionError("raw remote executor must never execute")


def setup(tmp_path, *, profile="inspect", collision=False, drift=False):
    runtime = RemoteJobRuntime(run_dir=tmp_path, run_id="root-run", limit=1,
                               deadline=datetime.now(UTC) + timedelta(seconds=10))
    requests = []

    def call(name, args):
        requests.append(name)
        assert name == "__job_shutdown__"
        return ToolResult(call_id="internal", tool_name=name, success=True,
            output={"closed": True, "active": False, "outcome_unknown": False})

    channel = SimpleNamespace(call=call, job_protocol={"version": 1,
        "logical_run_id": runtime.run_id, "worker_epoch": runtime.settings.worker_epoch,
        "host_owner_epoch": "f" * 32 if drift else runtime.settings.host_owner_epoch,
        "context_sha256": "a" * 64})
    tools = ToolRegistry([PreparedRemote()])
    if collision:
        register_job_tools(tools, None)
    options = dict(tools=tools, channel=channel, checkout_id="b" * 64, profile=profile,
                   default_timeout=10, abort_executor=lambda: None,
                   source_identity=lambda: JobSourceState(
                       product_diff_sha256="c" * 64, nonempty_diff=False))
    return runtime, requests, options


def test_remote_assembly_keeps_raw_schema_and_internal_controls_out_of_model(tmp_path):
    runtime, requests, options = setup(tmp_path)
    try:
        runtime.bind(**options)
        assert runtime.tools.names == ("run_tests", *JOB_TOOL_NAMES)
        assert not set(runtime.tools.names).intersection(JOB_CONTROL_CAPABILITIES)
        assert runtime.tools.get("run_tests").spec == options["tools"].get("run_tests").spec
        assert runtime.session.source_state().product_diff_sha256 == "c" * 64
        before = runtime.ledger.checkpoint_identity()
        with pytest.raises(JobError, match="profile_denied"):
            runtime.service.start(ToolCall(id="denied", name="run_tests", arguments={}))
        assert runtime.ledger.checkpoint_identity() == before and not requests
        assert not runtime.dispatcher.authorize("start_test_job", CallerOrigin.MODEL).allowed
        runtime.checkpoints.verify_saved(runtime.checkpoints.safe_identity())
        with pytest.raises(JobError, match="already_bound"):
            runtime.bind(**options)
    finally:
        assert runtime.close()
    assert runtime.close() and requests == ["__job_shutdown__"]


@pytest.mark.parametrize("reason,options", [
    ("tool_collision", {"collision": True}),
    ("channel_identity_mismatch", {"drift": True}),
])
def test_remote_bind_rejects_incompatible_channel_without_rpc(tmp_path, reason, options):
    runtime, requests, arguments = setup(tmp_path, **options)
    try:
        with pytest.raises(JobError, match=reason):
            runtime.bind(**arguments)
        assert not requests and runtime.ledger.held == runtime.ledger.charged == 0
    finally:
        assert runtime.close()


def test_bad_runtime_settings_release_root_ownership(tmp_path):
    with pytest.raises(ValidationError):
        RemoteJobRuntime(run_dir=tmp_path, run_id="root-run", limit=1,
                         deadline=datetime.now())
    with TestExecutionLedger(tmp_path, run_id="root-run", limit=1) as ledger:
        assert ledger.held == ledger.charged == 0
