"""Host-owned remote receipts and accounting; transport adapters never own test budgets."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from tracefix.checkpoint import CheckpointStore
from tracefix.jobs.domain import (
    TERMINAL_STATUSES,
    JobError,
    JobIdentity,
    JobRecord,
    JobStatus,
    select_deadline,
)
from tracefix.jobs.protocol import JobWorkerSettings, WorkerJobReceipt, WorkerStartRequest
from tracefix.jobs.test_budget import TestExecutionLedger
from tracefix.messages import ToolCall
from tracefix.tools.base import ToolResult


class JobChannel(Protocol):
    def call(self, name: str, arguments: dict) -> ToolResult: ...


def _digest(result: ToolResult) -> str:
    raw = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


class _RemoteEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record: JobRecord
    call_json: str = Field(max_length=65536)
    worker_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    worker_revision: StrictInt | None = Field(default=None, ge=0)
    result_json: str | None = Field(default=None, max_length=8 * 1024 * 1024)
    mode: Literal["background", "synchronous"]

    @property
    def result(self) -> ToolResult | None:
        return ToolResult.model_validate_json(self.result_json) if self.result_json else None

    @model_validator(mode="after")
    def provenance(self):
        call, identity = ToolCall.model_validate_json(self.call_json), self.record.identity
        if (call.name != "run_tests" or call.id != identity.call_id
                or hashlib.sha256(call.model_dump_json().encode()).hexdigest()
                != identity.invocation_sha256):
            raise ValueError("remote job call provenance mismatch")
        result = self.result
        if (self.record.outcome_sha256 != (_digest(result) if result is not None else None)
                or result is not None and (result.call_id != call.id
                                          or result.tool_name != "run_tests")):
            raise ValueError("remote job outcome provenance mismatch")
        return self


class RemoteJobService:
    """A new worker can read history but can never adopt or replay an old execution."""

    def __init__(self, record_dir: Path, *, settings: JobWorkerSettings,
                 checkout_id: str, ledger: TestExecutionLedger, channel: JobChannel,
                 prepare: Callable[[ToolCall], float], authorize: Callable[[ToolCall], None],
                 historical_read: Callable[..., dict], abort_executor: Callable[[], None]) -> None:
        if settings.host_owner_epoch != ledger.owner_epoch:
            raise JobError("remote_job_host_owner_mismatch")
        self.run_id, self.settings, self.ledger = settings.logical_run_id, settings, ledger
        self.channel, self.prepare, self.authorize = channel, prepare, authorize
        self.historical_read, self.abort_executor = historical_read, abort_executor
        self._lock = threading.RLock()
        self._accepting, self._faulted = True, False
        self._sequence, self._jobs = 0, {}
        self._deadline = time.monotonic() + (
            settings.session_deadline - datetime.now(UTC)).total_seconds()
        self.store = CheckpointStore(record_dir, {
            "kind": "remote-job-receipts-v1", "run_id": self.run_id,
            "checkout_id": checkout_id,
        })
        if self.store.path.exists():
            if self.store.path.stat().st_size > 8 * 1024 * 1024:
                raise JobError("remote_job_receipt_limit_exceeded")
            saved = self.store.load(current_identity=self.store.identity)
            if set(saved.payload) != {"jobs"} or not isinstance(saved.payload["jobs"], dict):
                raise JobError("remote_job_receipt_invalid")
            self._sequence = saved.sequence
            for job_id, value in saved.payload["jobs"].items():
                entry = _RemoteEntry.model_validate(value)
                entry.record.require_owner(self.run_id)
                execution = ledger.get(entry.record.identity.execution_id)
                if (job_id != entry.record.identity.job_id
                        or execution.call_id != entry.record.identity.call_id
                        or execution.invocation_sha256 != entry.record.identity.invocation_sha256):
                    raise JobError("remote_job_execution_reference_mismatch")
                if entry.record.status not in TERMINAL_STATUSES:
                    entry = self._unknown_entry(entry, execution.process_started)
                elif entry.record.status != JobStatus.OUTCOME_UNKNOWN and (
                    execution.phase not in {"settled", "not_started"}
                    or execution.process_started is not entry.record.process_started
                ):
                    raise JobError("remote_job_execution_observation_mismatch")
                self._jobs[job_id] = entry
            if saved.payload != self._payload(self._jobs):
                self._save(dict(self._jobs))

    @staticmethod
    def _payload(jobs):
        return {"jobs": {key: value.model_dump(mode="json") for key, value in jobs.items()}}

    def _save(self, jobs):
        payload = self._payload(jobs)
        if len(json.dumps(payload).encode()) > 8 * 1024 * 1024:
            self._faulted = True
            raise JobError("remote_job_receipt_limit_exceeded")
        try:
            self.store.save(payload, sequence=self._sequence + 1)
        except BaseException:
            self._faulted = True
            raise
        self._jobs, self._sequence = jobs, self._sequence + 1

    def _persist(self, entry):
        self._save({**self._jobs, entry.record.identity.job_id: entry})

    def _entry(self, job_id):
        if job_id not in self._jobs:
            raise JobError("job_not_available_in_this_run")
        return self._jobs[job_id]

    @property
    def active(self):
        return any(row.record.status not in TERMINAL_STATUSES for row in self._jobs.values())

    @property
    def has_unknown(self):
        return (self._faulted or self.ledger.has_unknown or any(
            row.record.status == JobStatus.OUTCOME_UNKNOWN for row in self._jobs.values()))

    @staticmethod
    def _unknown_entry(entry, started):
        record = JobRecord.model_validate({
            **entry.record.model_dump(), "revision": entry.record.revision + 1,
            "status": JobStatus.OUTCOME_UNKNOWN, "process_started": started,
            "tree_exit_confirmed": False, "outcome_sha256": None,
        })
        return entry.model_copy(update={"record": record, "result_json": None})

    def _unknown(self, entry):
        execution = self.ledger.get(entry.record.identity.execution_id)
        if execution.phase not in {"settled", "not_started", "unknown"}:
            execution = self.ledger.settle(execution.execution_id,
                                          process_started=execution.process_started,
                                          tree_exit_confirmed=False, outcome_known=False)
        self._persist(self._unknown_entry(entry, execution.process_started))

    def _receive(self, entry, response):
        if not response.success:
            raise JobError("remote_job_control_result_unknown")
        receipt = WorkerJobReceipt.model_validate(response.output)
        execution, original = receipt.execution, self.ledger.get(entry.record.identity.execution_id)
        if (receipt.worker_epoch != entry.worker_epoch or receipt.logical_run_id != self.run_id
                or execution.execution_id != original.execution_id
                or execution.owner_epoch != original.owner_epoch
                or execution.call_id != original.call_id
                or execution.invocation_sha256 != original.invocation_sha256):
            raise JobError("remote_job_receipt_identity_mismatch")
        record, result = receipt.job, receipt.result
        if record is None:
            if execution.phase != "not_started":
                raise JobError("remote_job_receipt_missing_job")
            result = ToolResult(call_id=original.call_id, tool_name="run_tests", success=False,
                                error=receipt.error_code or "remote_test_failed_to_start")
            record = JobRecord.model_validate({
                **entry.record.model_dump(), "revision": entry.record.revision + 1,
                "status": JobStatus.FAILED_TO_START, "process_started": False,
                "tree_exit_confirmed": True, "outcome_sha256": _digest(result),
            })
        if (record.identity != entry.record.identity or record.deadline > entry.record.deadline
                or record.created_at < entry.record.created_at
                or entry.worker_revision is not None and (
                    record.created_at != entry.record.created_at
                    or record.deadline != entry.record.deadline
                    or record.revision < entry.worker_revision
                    or record.revision == entry.worker_revision and record != entry.record)
                or entry.record.process_started is True and record.process_started is not True):
            raise JobError("remote_job_receipt_state_drift")
        if execution.process_started is True and original.process_started is not True:
            self.ledger.acknowledge_started(original.execution_id)
        if execution.phase in {"settled", "not_started", "unknown"}:
            self.ledger.settle(original.execution_id, process_started=execution.process_started,
                               tree_exit_confirmed=execution.tree_exit_confirmed,
                               outcome_known=execution.phase != "unknown")
        updated = entry.model_copy(update={
            "record": record, "worker_revision": record.revision,
            "result_json": result.model_dump_json() if result is not None else None,
        })
        self._persist(_RemoteEntry.model_validate_json(updated.model_dump_json()))
        return record

    def start(self, call: ToolCall, *, mode: str = "background") -> JobRecord:
        with self._lock:
            self.authorize(call)
            if not self._accepting or self.has_unknown:
                raise JobError("job_session_stopped_or_unknown")
            if self.active:
                raise JobError("checkout_test_already_active")
            if mode not in {"background", "synchronous"} or call.name != "run_tests":
                raise JobError("job_test_tool_or_mode_invalid")
            if time.monotonic() >= self._deadline:
                raise JobError("job_deadline_already_expired")
            call = ToolCall.model_validate_json(call.model_dump_json())
            timeout = self.prepare(call)
            now = datetime.now(UTC)
            deadline = select_deadline(now=now, timeout_seconds=timeout,
                                       default_timeout_seconds=timeout,
                                       session_deadline=self.settings.session_deadline,
                                       host_deadline=now + timedelta(seconds=max(
                                           0, self._deadline - time.monotonic())))
            execution = self.ledger.reserve(call_id=call.id,
                invocation_sha256=hashlib.sha256(call.model_dump_json().encode()).hexdigest())
            identity = JobIdentity(job_id="job_" + uuid4().hex, run_id=self.run_id,
                                   call_id=call.id, execution_id=execution.execution_id,
                                   invocation_sha256=execution.invocation_sha256)
            entry = _RemoteEntry(record=JobRecord(
                identity=identity, status=JobStatus.STARTING, revision=1, process_started=None,
                created_at=now, deadline=deadline), call_json=call.model_dump_json(),
                mode=mode, worker_epoch=self.settings.worker_epoch)
            try:
                self._persist(entry)
            except BaseException:
                self.ledger.settle(execution.execution_id, process_started=False,
                                   tree_exit_confirmed=True)
                raise
            execution = self.ledger.before_dispatch(execution.execution_id)
            request = WorkerStartRequest(version=1, worker_epoch=entry.worker_epoch,
                logical_run_id=self.run_id, job_id=identity.job_id, ticket=execution,
                call_json=entry.call_json, deadline=deadline, mode=mode)
            try:
                return self._receive(entry, self.channel.call(
                    "__job_start__", request.model_dump(mode="json")))
            except Exception:
                self._unknown(entry)
                return self._entry(identity.job_id).record

    def _query(self, job_id, operation):
        entry = self._entry(job_id)
        if entry.record.status in TERMINAL_STATUSES:
            return entry.record
        if entry.worker_epoch != self.settings.worker_epoch:
            raise JobError("remote_job_worker_epoch_not_current")
        try:
            return self._receive(entry, self.channel.call(operation, {
                "worker_epoch": entry.worker_epoch, "job_id": job_id}))
        except Exception:
            self._unknown(entry)
            return self._entry(job_id).record

    def get(self, job_id):
        with self._lock:
            return self._query(job_id, "__job_status__")

    def result(self, job_id):
        with self._lock:
            return self._entry(job_id).result

    def cancel(self, job_id):
        with self._lock:
            return self._query(job_id, "__job_cancel__")

    def read(self, job_id, *, stream="stdout", offset=0, limit=4096):
        from tracefix.jobs.protocol import WorkerReadQuery

        with self._lock:
            entry = self._entry(job_id)
            query = WorkerReadQuery(worker_epoch=entry.worker_epoch, job_id=job_id,
                                    stream=stream, offset=offset, limit=limit)
            if entry.worker_epoch != self.settings.worker_epoch:
                return self.historical_read(entry.record, stream=stream, offset=offset, limit=limit)
            response = self.channel.call("__job_read__", query.model_dump(mode="json"))
            if not response.success:
                raise JobError("remote_job_output_unavailable")
            page = response.output
            if not isinstance(page, dict) or page.get("job_id") != job_id:
                raise JobError("remote_job_output_identity_mismatch")
            return page

    def background_completions(self):
        with self._lock:
            for job_id in tuple(self._jobs):
                self._query(job_id, "__job_status__")
            return tuple((entry.record, entry.result) for entry in self._jobs.values()
                         if entry.mode == "background" and entry.result is not None
                         and entry.record.status in TERMINAL_STATUSES)

    def progress_receipts(self):
        return tuple(entry.record for entry in self._jobs.values())

    def checkpoint_identity(self):
        with self._lock:
            if self.active or self.has_unknown:
                raise JobError("job_checkpoint_requires_known_quiescence")
            if not self.store.path.exists() and not self._jobs and not self._sequence:
                return {"sequence": 0, "payload_sha256": None}
            saved = self.store.load(current_identity=self.store.identity)
            if saved.sequence != self._sequence or saved.payload != self._payload(self._jobs):
                raise JobError("remote_job_receipt_changed_outside_owner")
            return {"sequence": saved.sequence, "payload_sha256": saved.payload_sha256}

    @staticmethod
    def inspect_checkpoint_identity(record_dir: Path, *, run_id: str,
                                    checkout_id: str) -> dict[str, object]:
        """No transport, ownership, repair or budget mutation on a read-only inspection."""
        store = CheckpointStore(record_dir, {
            "kind": "remote-job-receipts-v1", "run_id": run_id, "checkout_id": checkout_id,
        })
        if not store.path.exists():
            return {"sequence": 0, "payload_sha256": None}
        if store.path.stat().st_size > 8 * 1024 * 1024:
            raise JobError("remote_job_receipt_limit_exceeded")
        saved = store.load(current_identity=store.identity)
        if set(saved.payload) != {"jobs"} or not isinstance(saved.payload["jobs"], dict):
            raise JobError("remote_job_receipt_invalid")
        for job_id, value in saved.payload["jobs"].items():
            entry = _RemoteEntry.model_validate(value)
            entry.record.require_owner(run_id)
            if (job_id != entry.record.identity.job_id
                    or entry.record.status not in TERMINAL_STATUSES
                    or entry.record.status == JobStatus.OUTCOME_UNKNOWN):
                raise JobError("remote_job_receipt_pending_unknown_or_invalid")
        return {"sequence": saved.sequence, "payload_sha256": saved.payload_sha256}

    def run_synchronously(self, call):
        record = self.start(call, mode="synchronous")
        until = min(self._deadline, time.monotonic() + max(
            0, (record.deadline - datetime.now(UTC)).total_seconds())) + 20
        while record.status not in TERMINAL_STATUSES and time.monotonic() < until:
            time.sleep(0.02)
            record = self.get(record.identity.job_id)
        if record.status not in TERMINAL_STATUSES:
            with self._lock:
                self._unknown(self._entry(record.identity.job_id))
        result = self.result(record.identity.job_id)
        if result is None:
            raise JobError("synchronous_test_outcome_unknown")
        return result

    def shutdown(self, timeout=20.0):
        with self._lock:
            self._accepting = False
            job_ids = tuple(self._jobs)
        for job_id in job_ids:
            self.cancel(job_id)
        until = time.monotonic() + max(0, min(timeout, 20.0))
        while self.active and not self.has_unknown and time.monotonic() < until:
            for job_id in job_ids:
                self.get(job_id)
            if self.active:
                time.sleep(0.02)
        closed = False
        try:
            response = self.channel.call("__job_shutdown__", {
                "worker_epoch": self.settings.worker_epoch})
            closed = (response.success and response.output == {
                "closed": True, "active": False, "outcome_unknown": False})
        except Exception:
            pass
        if not closed or self.active or self.has_unknown:
            with self._lock:
                for entry in tuple(self._jobs.values()):
                    if entry.record.status not in TERMINAL_STATUSES:
                        self._unknown(entry)
            self.abort_executor()
            return False
        return True
