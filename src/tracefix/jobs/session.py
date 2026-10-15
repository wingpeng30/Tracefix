"""Small main-thread port; no worker may mutate an agent or its message history."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from tracefix.jobs.domain import JobRecord
from tracefix.jobs.ports import JobCompletion, JobSourceState
from tracefix.jobs.test_budget import TestExecutionLedger
from tracefix.tools.base import ToolResult


class JobSessionService(Protocol):
    @property
    def active(self) -> bool: ...
    @property
    def has_unknown(self) -> bool: ...
    def background_completions(self) -> tuple[tuple[JobRecord, ToolResult], ...]: ...
    def shutdown(self) -> bool: ...


class ManagedAgentJobSession:
    """Consume host-owned receipts; local and remote workers never mutate history."""

    def __init__(self, service: JobSessionService, ledger: TestExecutionLedger,
                 source_identity: Callable[[], JobSourceState]) -> None:
        self.service, self.ledger = service, ledger
        self._source_identity = source_identity

    @property
    def active(self) -> bool:
        return self.service.active

    @property
    def has_unknown(self) -> bool:
        return self.service.has_unknown

    @property
    def test_runs(self) -> int:
        return self.ledger.charged

    def completions(self) -> tuple[JobCompletion, ...]:
        return tuple(JobCompletion(record, result)
                     for record, result in self.service.background_completions())

    def source_state(self) -> JobSourceState:
        return self._source_identity()

    def shutdown(self) -> bool:
        return self.service.shutdown()


class LocalAgentJobSession(ManagedAgentJobSession):
    """Compatibility name for the existing local assembly."""
