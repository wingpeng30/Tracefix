"""Host runtime assembly through small transport ports, independent of DockerBackend."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from tracefix.capabilities import (
    CallerOrigin,
    CapabilityCatalog,
    CapabilityPolicy,
    ExecutionProfile,
    ToolDispatcher,
)
from tracefix.jobs.artifact_reader import RemoteJobArtifactReader
from tracefix.jobs.checkpoints import JobCheckpointCoordinator
from tracefix.jobs.constants import JOB_CONTROL_CAPABILITIES
from tracefix.jobs.domain import JobError
from tracefix.jobs.policy import JobActivityGuard
from tracefix.jobs.ports import JobSourceState
from tracefix.jobs.protocol import JobProtocolHello, JobWorkerSettings
from tracefix.jobs.remote import JobChannel, RemoteJobService
from tracefix.jobs.session import ManagedAgentJobSession
from tracefix.jobs.test_budget import TestExecutionLedger
from tracefix.messages import ToolCall
from tracefix.tools.base import ToolRegistry
from tracefix.tools.jobs import JOB_TOOL_NAMES, ManagedRunTestsTool, register_job_tools


class RemoteJobRuntime:
    """Own the root budget before executor startup; bind only a verified channel later."""

    def __init__(self, *, run_dir: Path, run_id: str, limit: int, deadline: datetime) -> None:
        self.run_dir, self.run_id, self.deadline = run_dir, run_id, deadline
        self.ledger = TestExecutionLedger(run_dir, run_id=run_id, limit=limit)
        self.service = None
        self._closed = False
        try:
            self.settings = JobWorkerSettings(logical_run_id=run_id, worker_epoch=uuid4().hex,
                host_owner_epoch=self.ledger.owner_epoch, session_deadline=deadline)
        except BaseException:
            self.ledger.close()
            raise

    def bind(self, *, tools: ToolRegistry, channel: JobChannel, checkout_id: str,
             source_identity: Callable[[], JobSourceState], abort_executor: Callable[[], None],
             profile: ExecutionProfile | str, default_timeout: float,
             extra_capabilities: Iterable[str] = ()) -> None:
        if self.service is not None or self._closed:
            raise JobError("remote_job_runtime_already_bound_or_closed")
        if any(name in tools.names for name in JOB_TOOL_NAMES):
            raise JobError("background_job_tool_collision")
        hello = JobProtocolHello.model_validate(getattr(channel, "job_protocol", None))
        if (hello.logical_run_id != self.run_id or hello.worker_epoch != self.settings.worker_epoch
                or hello.host_owner_epoch != self.settings.host_owner_epoch):
            raise JobError("remote_job_runtime_channel_identity_mismatch")
        executor = tools.get("run_tests")
        self.policy = CapabilityPolicy(CapabilityCatalog.for_tool_names((
            *tools.names, *JOB_TOOL_NAMES, *JOB_CONTROL_CAPABILITIES, *extra_capabilities)),
            ExecutionProfile(profile))

        def authorize(call):
            decision = self.policy.authorize(call.name, CallerOrigin.MODEL)
            if not decision.allowed:
                raise JobError(decision.reason_code)

        def prepare(call: ToolCall):
            executor.prepare(call)
            return call.arguments.get("timeout_seconds", default_timeout)

        def read_history(record, **kwargs):
            return reader.read(record, **kwargs)

        service = RemoteJobService(self.run_dir / "jobs", settings=self.settings,
            checkout_id=checkout_id, ledger=self.ledger, channel=channel, prepare=prepare,
            authorize=authorize, historical_read=read_history, abort_executor=abort_executor)
        reader = RemoteJobArtifactReader(channel, run_id=self.run_id,
                                          result_provider=service.result)
        self.tools = ToolRegistry(ManagedRunTestsTool(tool, service) if tool is executor else tool
                                  for tool in tools)
        register_job_tools(self.tools, service)
        self.service = service
        self.dispatcher = ToolDispatcher(self.policy, execution_guard=JobActivityGuard(service))
        self.session = ManagedAgentJobSession(service, self.ledger, source_identity)
        self.checkpoints = JobCheckpointCoordinator(self.run_dir, run_id=self.run_id,
                                                    service=service, ledger=self.ledger)

    def close(self) -> bool:
        if self._closed:
            return not self.ledger.has_unknown and (self.service is None
                                                    or not self.service.has_unknown)
        try:
            return (self.service.shutdown() if self.service is not None
                    else not self.ledger.has_unknown and not self.ledger.pending)
        finally:
            self._closed = True
            self.ledger.close()
