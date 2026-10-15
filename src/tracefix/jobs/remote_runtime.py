"""Host runtime assembly through small transport ports, independent of DockerBackend."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
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
from tracefix.jobs.artifact_seed import JobArtifactChain
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
        self.artifacts = JobArtifactChain(run_id)
        self._closed = False
        self._close_result = False
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

    def validation_settings(self) -> JobWorkerSettings:
        if self._closed or self.ledger.has_unknown:
            raise JobError("validation_root_stopped_or_unknown")
        return self.settings.model_copy(update={"worker_epoch": uuid4().hex})

    @contextmanager
    def validation_executor(self, executor, *, channel: JobChannel, settings: JobWorkerSettings,
                            record_dir: Path, checkout_id: str,
                            abort_executor: Callable[[], None], default_timeout: float):
        """A disposable checkout receives host tickets from the same root authority."""
        for name in ("validation_baseline", "run_tests"):
            decision = self.dispatcher.authorize(name, CallerOrigin.VALIDATION_GATE)
            if not decision.allowed:
                raise JobError(decision.reason_code)
        hello = JobProtocolHello.model_validate(getattr(channel, "job_protocol", None))
        if (hello.logical_run_id != self.run_id or hello.worker_epoch != settings.worker_epoch
                or hello.host_owner_epoch != self.ledger.owner_epoch
                or settings.host_owner_epoch != self.ledger.owner_epoch
                or settings.logical_run_id != self.run_id
                or settings.session_deadline > self.deadline):
            raise JobError("validation_job_channel_identity_mismatch")

        def authorize(call):
            decision = self.dispatcher.authorize(call.name, CallerOrigin.VALIDATION_GATE)
            if not decision.allowed:
                raise JobError(decision.reason_code)

        def prepare(call):
            executor.prepare(call)
            return call.arguments.get("timeout_seconds", default_timeout)

        def no_history(*args, **kwargs):
            raise JobError("validation_executor_has_no_historical_worker")

        service = RemoteJobService(record_dir, settings=settings, checkout_id=checkout_id,
            ledger=self.ledger, channel=channel, prepare=prepare, authorize=authorize,
            historical_read=no_history, abort_executor=abort_executor)
        primary = None
        try:
            yield ManagedRunTestsTool(executor, service)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                if not service.shutdown():
                    raise JobError("validation_job_cleanup_or_outcome_unknown")
            except Exception:
                if primary is None:
                    raise
                primary.add_note("validation_job_cleanup_or_outcome_unknown")

    def close(self) -> bool:
        if self._closed:
            return self._close_result
        try:
            self._close_result = (self.service.shutdown() if self.service is not None
                                  else not self.ledger.has_unknown and not self.ledger.pending)
            return self._close_result
        finally:
            self._closed = True
            self.ledger.close()
