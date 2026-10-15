"""Small main-thread port; no worker may mutate an agent or its message history."""

from __future__ import annotations

from collections.abc import Callable

from tracefix.jobs.ports import JobCompletion, JobSourceState
from tracefix.jobs.service import JobService
from tracefix.jobs.test_budget import TestExecutionLedger


class LocalAgentJobSession:
    """Adapter for local execution; the core consumes only AgentJobSession."""

    def __init__(self, service: JobService, ledger: TestExecutionLedger,
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
