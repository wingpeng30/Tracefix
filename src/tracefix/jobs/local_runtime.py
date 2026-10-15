"""Local runtime assembly; domain services know nothing about Runner or Docker."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from tracefix.artifacts import ArtifactStore
from tracefix.capabilities import (
    CallerOrigin,
    CapabilityCatalog,
    CapabilityPolicy,
    ExecutionProfile,
    ToolDispatcher,
)
from tracefix.jobs.checkpoints import JobCheckpointCoordinator
from tracefix.jobs.domain import JobError
from tracefix.jobs.policy import JobActivityGuard
from tracefix.jobs.ports import JobSourceState
from tracefix.jobs.process_tree import LinuxCgroupScope, ProcessTreeScope, WindowsJobScope
from tracefix.jobs.service import JobService
from tracefix.jobs.session import LocalAgentJobSession
from tracefix.jobs.test_budget import TestExecutionLedger
from tracefix.messages import ToolCall
from tracefix.tools.base import ToolRegistry
from tracefix.tools.builtin import RunTestsTool
from tracefix.tools.jobs import JOB_TOOL_NAMES, ManagedRunTestsTool, register_job_tools


def local_scope_factory(delegated_cgroup: Path | None = None) -> Callable[[], ProcessTreeScope]:
    """Use only host-supplied delegation; no environment probing or privilege fallback."""
    if os.name == "nt":
        if delegated_cgroup is not None:
            raise JobError("cgroup_configuration_on_windows")
        return WindowsJobScope
    return lambda: LinuxCgroupScope(delegated_cgroup)


class LocalJobRuntime:
    """Own one ledger, prepared executor, job service and main-thread session port."""

    def __init__(
        self, *, tools: ToolRegistry, artifacts: ArtifactStore, run_dir: Path,
        run_id: str, checkout_id: str, limit: int, deadline: datetime,
        source_identity: Callable[[], JobSourceState], profile: ExecutionProfile | str,
        extra_capabilities: Iterable[str] = (), delegated_cgroup: Path | None = None,
        scope_factory: Callable[[], ProcessTreeScope] | None = None,
        baseline_runs: int = 0,
    ) -> None:
        executor = tools.get("run_tests")
        if not isinstance(executor, RunTestsTool):
            raise JobError("background_jobs_require_prepared_test_executor")
        if any(name in tools.names for name in JOB_TOOL_NAMES):
            raise JobError("background_job_tool_collision")
        if not artifacts.streaming_enabled:
            raise JobError("background_jobs_require_streaming_artifacts")
        self.artifacts, self.deadline = artifacts, deadline
        self.policy = CapabilityPolicy(CapabilityCatalog.for_tool_names(
            (*tools.names, *JOB_TOOL_NAMES, *extra_capabilities)), ExecutionProfile(profile))
        self.scope_factory = scope_factory or local_scope_factory(delegated_cgroup)
        # Fail before ledger writes or model construction if strict ownership is unavailable.
        probe = self.scope_factory()
        if not probe.close():
            raise JobError("process_tree_scope_preflight_failed")
        self.ledger = TestExecutionLedger(run_dir, run_id=run_id, limit=limit,
                                          baseline_runs=baseline_runs)
        try:
            self.service = JobService(
                run_dir / "jobs", run_id=run_id, checkout_id=checkout_id, ledger=self.ledger,
                artifact_store=artifacts, test_tool=executor, authorize=self._authorize_test,
                scope_factory=self.scope_factory, evaluate=executor.evaluate_job_result,
                verify_source=executor.verify_prepared_source, session_deadline=deadline,
            )
            self.tools = ToolRegistry(
                ManagedRunTestsTool(tool, self.service) if tool is executor else tool
                for tool in tools)
            register_job_tools(self.tools, self.service)
            self.session = LocalAgentJobSession(self.service, self.ledger, source_identity)
            self.checkpoints = JobCheckpointCoordinator(run_dir, run_id=run_id,
                                                        service=self.service, ledger=self.ledger)
            self.dispatcher = ToolDispatcher(self.policy,
                                             execution_guard=JobActivityGuard(self.service))
        except BaseException:
            self.ledger.close()
            raise

    def _authorize_test(self, call: ToolCall) -> None:
        decision = self.policy.authorize(call.name, CallerOrigin.MODEL)
        if not decision.allowed:
            raise JobError(decision.reason_code)

    @contextmanager
    def validation_executor(self, executor: RunTestsTool, *, record_dir: Path,
                            checkout_id: str) -> Iterator[ManagedRunTestsTool]:
        """A fresh validation checkout uses the root ledger and artifact quotas."""
        decision = self.dispatcher.authorize("validation_baseline", CallerOrigin.VALIDATION_GATE)
        if not decision.allowed:
            raise JobError(decision.reason_code)

        def authorize(call):
            decision = self.dispatcher.authorize(call.name, CallerOrigin.VALIDATION_GATE)
            if not decision.allowed:
                raise JobError(decision.reason_code)

        service = JobService(
            record_dir, run_id=self.service.run_id, checkout_id=checkout_id, ledger=self.ledger,
            artifact_store=self.artifacts, test_tool=executor, authorize=authorize,
            scope_factory=self.scope_factory, evaluate=executor.evaluate_job_result,
            verify_source=executor.verify_prepared_source, session_deadline=self.deadline)
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
        try:
            return self.service.shutdown()
        finally:
            self.ledger.close()
