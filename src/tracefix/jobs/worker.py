"""Synchronous control methods around asynchronous execution; workers emit no RPC frames."""

from __future__ import annotations

from collections.abc import Callable

from tracefix.jobs.domain import JobError
from tracefix.jobs.protocol import (
    WorkerJobQuery,
    WorkerJobReceipt,
    WorkerStartRequest,
    WorkerTicketMirror,
)
from tracefix.jobs.service import JobService


class JobWorkerController:
    def __init__(self, *, service: JobService, tickets: WorkerTicketMirror,
                 authorize: Callable[[str], None]) -> None:
        if service.run_id != tickets.run_id or service.ledger is not tickets:
            raise JobError("worker_service_ticket_identity_mismatch")
        self.service, self.tickets, self.authorize = service, tickets, authorize

    def _receipt(self, execution_id: str, job_id: str,
                 error_code: str | None = None) -> WorkerJobReceipt:
        job = result = None
        if any(row.identity.job_id == job_id for row in self.service.progress_receipts()):
            job, result = self.service.receipt(job_id)
        return WorkerJobReceipt(
            worker_epoch=self.tickets.worker_epoch, logical_run_id=self.tickets.run_id,
            execution=self.tickets.get(execution_id), job=job,
            result_json=result.model_dump_json() if result is not None else None,
            error_code=error_code)

    def start(self, request: WorkerStartRequest) -> WorkerJobReceipt:
        self.authorize("job_start_control")
        self.tickets.arm(request)
        error_code = None
        try:
            self.service.start(request.call, mode=request.mode, host_deadline=request.deadline,
                               assigned_job_id=request.job_id)
        except Exception as exc:
            error_code = exc.reason_code if isinstance(exc, JobError) else "worker_start_rejected"
            try:
                self.tickets.reject_unconsumed()
            except JobError as rejected:
                if rejected.reason_code != "worker_ticket_already_consumed":
                    raise
        return self._receipt(request.ticket.execution_id, request.job_id, error_code)

    def _query(self, query: WorkerJobQuery, capability: str) -> None:
        self.authorize(capability)
        if query.worker_epoch != self.tickets.worker_epoch:
            raise JobError("worker_query_epoch_mismatch")
        self.service.get(query.job_id)

    def status(self, query: WorkerJobQuery) -> WorkerJobReceipt:
        self._query(query, "job_status_control")
        record = self.service.get(query.job_id)
        return self._receipt(record.identity.execution_id, query.job_id)

    def cancel(self, query: WorkerJobQuery) -> WorkerJobReceipt:
        self._query(query, "job_cancel_control")
        record = self.service.cancel(query.job_id)
        return self._receipt(record.identity.execution_id, query.job_id)

    def read(self, query: WorkerJobQuery, *, stream: str = "stdout",
             offset: int = 0, limit: int = 4096) -> dict:
        self._query(query, "job_read_control")
        return self.service.read(query.job_id, stream=stream, offset=offset, limit=limit)

    def shutdown(self, *, worker_epoch: str) -> dict[str, bool]:
        self.authorize("job_shutdown_control")
        if worker_epoch != self.tickets.worker_epoch:
            raise JobError("worker_query_epoch_mismatch")
        closed = self.service.shutdown()
        return {"closed": closed, "active": self.service.active,
                "outcome_unknown": self.service.has_unknown}
