"""Host-owned job lifecycle. Worker receipts are consumed only at closed tool batches."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from tracefix.artifacts import ArtifactStore
from tracefix.checkpoint import CheckpointStore
from tracefix.jobs.capture import LiveCaptureResult, LiveProcessCapture
from tracefix.jobs.domain import (
    TERMINAL_STATUSES,
    JobError,
    JobIdentity,
    JobRecord,
    JobStatus,
    select_deadline,
    transition,
)
from tracefix.jobs.process_tree import ProcessTreeScope
from tracefix.jobs.test_budget import TestExecution
from tracefix.messages import ToolCall
from tracefix.test_execution import PreparedTest
from tracefix.tools.base import ToolResult


class TestPreparation(Protocol):
    def prepare(self, call: ToolCall) -> tuple[list[str], float]: ...
    def prepare_test(self, call: ToolCall) -> PreparedTest: ...


class TestExecutionAccounting(Protocol):
    @property
    def has_unknown(self) -> bool: ...

    def reserve(self, *, call_id: str, invocation_sha256: str) -> TestExecution: ...
    def get(self, execution_id: str) -> TestExecution: ...
    def before_dispatch(self, execution_id: str) -> TestExecution: ...
    def acknowledge_started(self, execution_id: str) -> TestExecution: ...
    def settle(self, execution_id: str, *, process_started: bool | None,
               tree_exit_confirmed: bool, outcome_known: bool = True) -> TestExecution: ...


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record: JobRecord
    capture_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    result: ToolResult | None = None
    mode: Literal["background", "synchronous"] = "background"


def _result_sha(result: ToolResult) -> str:
    return hashlib.sha256(json.dumps(result.model_dump(mode="json"), ensure_ascii=False,
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class JobService:
    """One active test per owned checkout; no queue, daemon or recovery re-dispatch."""

    def __init__(
        self, record_dir: Path, *, run_id: str, checkout_id: str,
        ledger: TestExecutionAccounting, artifact_store: ArtifactStore,
        test_tool: TestPreparation, authorize: Callable[[ToolCall], None],
        scope_factory: Callable[[], ProcessTreeScope],
        evaluate: Callable[[PreparedTest, LiveCaptureResult], ToolResult],
        verify_source: Callable[[PreparedTest], None], session_deadline: datetime,
    ) -> None:
        self.run_id, self.ledger, self.artifacts = run_id, ledger, artifact_store
        self.test_tool, self.authorize = test_tool, authorize
        self.scope_factory = scope_factory
        self.evaluate, self.verify_source = evaluate, verify_source
        self.session_deadline = session_deadline
        self._root_deadline = time.monotonic() + (
            session_deadline - datetime.now(UTC)).total_seconds()
        self._lock = threading.RLock()
        self._accepting, self._faulted = True, False
        self._jobs: dict[str, _Entry] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._captures: dict[str, LiveProcessCapture] = {}
        self._deadlines: dict[str, float] = {}
        self.store = CheckpointStore(record_dir, {
            "kind": "job-receipts-v1", "run_id": run_id, "checkout_id": checkout_id,
        })
        self._sequence = 0
        if self.store.path.exists():
            if self.store.path.stat().st_size > 8 * 1024 * 1024:
                raise JobError("job_receipt_limit_exceeded")
            saved = self.store.load(current_identity=self.store.identity)
            if set(saved.payload) != {"jobs"} or not isinstance(saved.payload["jobs"], dict):
                raise JobError("job_receipt_invalid")
            self._sequence = saved.sequence
            recovered = False
            for job_id, payload in saved.payload["jobs"].items():
                entry = _Entry.model_validate(payload)
                entry.record.require_owner(run_id)
                if job_id != entry.record.identity.job_id:
                    raise JobError("job_receipt_identity_mismatch")
                execution = self.ledger.get(entry.record.identity.execution_id)
                if (execution.call_id != entry.record.identity.call_id
                        or execution.invocation_sha256 != entry.record.identity.invocation_sha256):
                    raise JobError("job_execution_reference_mismatch")
                if entry.record.outcome_sha256 != (
                    _result_sha(entry.result) if entry.result is not None else None
                ):
                    raise JobError("job_outcome_receipt_mismatch")
                record = entry.record
                if record.status == JobStatus.COMPLETED and execution.phase != "settled":
                    raise JobError("job_completed_without_settled_execution")
                if (record.status in TERMINAL_STATUSES
                        and record.status != JobStatus.OUTCOME_UNKNOWN
                        and record.process_started is not execution.process_started):
                    raise JobError("job_execution_observation_mismatch")
                if record.status not in TERMINAL_STATUSES:
                    recovered = True
                    if record.status == JobStatus.NOT_STARTED:
                        record = transition(record, JobStatus.STARTING, process_started=None)
                    record = transition(record, JobStatus.OUTCOME_UNKNOWN,
                                        process_started=execution.process_started)
                    entry = entry.model_copy(update={"record": record})
                self._jobs[job_id] = entry
            if recovered:
                self._save(dict(self._jobs))

    def _save(self, jobs: dict[str, _Entry]) -> None:
        payload = {"jobs": {key: row.model_dump(mode="json") for key, row in jobs.items()}}
        if len(json.dumps(payload).encode()) > 8 * 1024 * 1024:
            self._faulted = True
            raise JobError("job_receipt_limit_exceeded")
        try:
            self.store.save(payload, sequence=self._sequence + 1)
        except BaseException:
            self._faulted = True
            raise
        self._jobs, self._sequence = jobs, self._sequence + 1

    def _persist(self, entry: _Entry) -> None:
        self._save({**self._jobs, entry.record.identity.job_id: entry})

    def _entry(self, job_id: str) -> _Entry:
        try:
            row = self._jobs[job_id]
        except (KeyError, TypeError) as exc:
            raise JobError("job_not_available_in_this_run") from exc
        row.record.require_owner(self.run_id)
        return row

    @property
    def active(self) -> bool:
        with self._lock:
            return any(row.record.status not in TERMINAL_STATUSES for row in self._jobs.values())

    @property
    def has_unknown(self) -> bool:
        with self._lock:
            return self._faulted or self.ledger.has_unknown or any(
                row.record.status == JobStatus.OUTCOME_UNKNOWN for row in self._jobs.values())

    def start(self, call: ToolCall, *, mode: str = "background") -> JobRecord:
        with self._lock:
            if mode not in {"background", "synchronous"}:
                raise JobError("job_execution_mode_invalid")
            if not self._accepting or self.has_unknown:
                raise JobError("job_session_stopped_or_unknown")
            if self.active:
                raise JobError("checkout_test_already_active")
            # Direct Python calls have the same mandatory authorization as model calls.
            self.authorize(call)
            if call.name != "run_tests":
                raise JobError("job_test_tool_required")
            call = ToolCall.model_validate_json(call.model_dump_json())
            _, timeout = self.test_tool.prepare(call)
            now = datetime.now(UTC)
            deadline = select_deadline(now=now, timeout_seconds=timeout,
                                       default_timeout_seconds=timeout,
                                       session_deadline=self.session_deadline)
            monotonic_deadline = min(self._root_deadline, time.monotonic() + timeout)
            identity = JobIdentity(
                job_id="job_" + uuid4().hex, run_id=self.run_id, call_id=call.id,
                execution_id="0" * 32,
                invocation_sha256=hashlib.sha256(call.model_dump_json().encode()).hexdigest(),
            )
            ticket = self.ledger.reserve(call_id=call.id,
                                         invocation_sha256=identity.invocation_sha256)
            scope = capture = None
            try:
                prepared = self.test_tool.prepare_test(call)
                scope = self.scope_factory()
                capture = LiveProcessCapture(
                    self.artifacts, call_id=call.id,
                    source_identity={"product_diff_sha256_before": prepared.source_sha256_before},
                )
                identity = identity.model_copy(update={"execution_id": ticket.execution_id})
                record = JobRecord(identity=identity, created_at=now, deadline=deadline)
                record = transition(record, JobStatus.STARTING, process_started=None)
                self._persist(_Entry(record=record, capture_id=capture.reservation.capture_id,
                                     mode=mode))
                self._captures[identity.job_id] = capture
                self._cancels[identity.job_id] = threading.Event()
                self._deadlines[identity.job_id] = monotonic_deadline
                self.ledger.before_dispatch(ticket.execution_id)
            except BaseException as exc:
                cleaned = True
                if scope is not None:
                    try:
                        cleaned = scope.close()
                    except Exception:
                        cleaned = False
                # No worker exists yet. Even if the dispatch-intent write failed, no test ran.
                try:
                    self.ledger.settle(ticket.execution_id, process_started=False,
                                       tree_exit_confirmed=cleaned)
                except Exception:
                    self._faulted = True
                if capture is not None:
                    empty = capture.seal_unstarted(tree_exit_confirmed=cleaned,
                                                  reason=type(exc).__name__)
                    if identity.job_id in self._jobs:
                        try:
                            outcome = self.evaluate(prepared, empty)
                            status = (JobStatus.FAILED_TO_START if cleaned
                                      else JobStatus.OUTCOME_UNKNOWN)
                            entry = self._entry(identity.job_id)
                            record = transition(entry.record, status, process_started=False,
                                                tree_exit_confirmed=cleaned,
                                                outcome_sha256=_result_sha(outcome))
                            self._persist(entry.model_copy(update={"record": record,
                                                                   "result": outcome}))
                        except Exception:
                            self._faulted = True
                raise
            worker = threading.Thread(target=self._run,
                                      args=(identity.job_id, prepared, scope, monotonic_deadline),
                                      name="tracefix-test-job", daemon=True)
            self._workers[identity.job_id] = worker
            try:
                worker.start()
            except BaseException:
                scope.close()
                self._faulted = True
                # Thread creation failures are not authority to replay a dispatched call.
                raise
            return record

    def _run(self, job_id: str, prepared: PreparedTest,
             scope: ProcessTreeScope, deadline: float) -> None:
        def started() -> None:
            execution_id = self._entry(job_id).record.identity.execution_id
            self.ledger.acknowledge_started(execution_id)
            with self._lock:
                entry = self._entry(job_id)
                status = (JobStatus.CANCELLING if entry.record.status == JobStatus.CANCELLING
                          else JobStatus.RUNNING)
                record = transition(entry.record, status, process_started=True)
                self._persist(entry.model_copy(update={"record": record}))

        try:
            try:
                self.verify_source(prepared)
            except Exception as exc:
                cleaned = scope.close()
                result = self._captures[job_id].seal_unstarted(
                    tree_exit_confirmed=cleaned, reason=type(exc).__name__)
            else:
                result = self._captures[job_id].run(
                    prepared.command, cwd=prepared.workspace, env=dict(prepared.environment),
                    scope=scope, cancel=self._cancels[job_id], deadline=deadline,
                    on_started=started,
                )
            outcome = self.evaluate(prepared, result)
            current = self._entry(job_id)
            if current.mode == "synchronous":
                outcome = outcome.model_copy(deep=True)
                outcome.metadata.update({
                    "managed_job_id": job_id,
                    "execution_id": current.record.identity.execution_id,
                    "process_started": result.process_started,
                    "tree_exit_confirmed": result.tree_exit_confirmed,
                })
            known = result.tree_exit_confirmed and (
                result.process_started is False or result.process_error is None)
            self.ledger.settle(self._entry(job_id).record.identity.execution_id,
                               process_started=result.process_started,
                               tree_exit_confirmed=result.tree_exit_confirmed, outcome_known=known)
            with self._lock:
                entry = self._entry(job_id)
                if not known or result.process_started is None:
                    status = JobStatus.OUTCOME_UNKNOWN
                elif result.deadline_exceeded or (
                    result.process_started is False and time.monotonic() >= deadline
                ):
                    status = JobStatus.DEADLINE_EXCEEDED
                elif result.cancelled or (
                    result.process_started is False and self._cancels[job_id].is_set()
                ):
                    status = JobStatus.CANCELLED
                elif result.process_started is False:
                    status = JobStatus.FAILED_TO_START
                else:
                    status = JobStatus.COMPLETED
                record = transition(entry.record, status, process_started=result.process_started,
                                    tree_exit_confirmed=result.tree_exit_confirmed,
                                    outcome_sha256=_result_sha(outcome))
                self._persist(entry.model_copy(update={"record": record, "result": outcome}))
        except Exception:
            # Persistence/evaluator failure cannot turn a real execution into a refundable start.
            try:
                cleaned = scope.close()
            except Exception:
                cleaned = False
            with self._lock:
                self._faulted = True
                entry = self._entry(job_id)
                try:
                    self.ledger.settle(entry.record.identity.execution_id,
                                       process_started=scope.process_started,
                                       tree_exit_confirmed=cleaned, outcome_known=False)
                except Exception:
                    pass
                try:
                    record = transition(entry.record, JobStatus.OUTCOME_UNKNOWN,
                                        process_started=scope.process_started,
                                        tree_exit_confirmed=cleaned)
                    self._persist(entry.model_copy(update={"record": record}))
                except Exception:
                    pass

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            return self._entry(job_id).record

    def result(self, job_id: str) -> ToolResult | None:
        with self._lock:
            result = self._entry(job_id).result
            return result.model_copy(deep=True) if result is not None else None

    def background_completions(self) -> tuple[tuple[JobRecord, ToolResult], ...]:
        with self._lock:
            return tuple((entry.record, entry.result.model_copy(deep=True))
                         for entry in self._jobs.values()
                         if entry.mode == "background" and entry.result is not None
                         and entry.record.status in TERMINAL_STATUSES)

    def checkpoint_identity(self) -> dict[str, object]:
        """Only a quiescent, known receipt set can belong to a safe checkpoint."""
        with self._lock:
            if self.active or self.has_unknown:
                raise JobError("job_checkpoint_requires_known_quiescence")
            if not self.store.path.exists():
                if self._jobs or self._sequence:
                    raise JobError("job_receipt_checkpoint_missing")
                return {"sequence": 0, "payload_sha256": None}
            snapshot = self.store.load(current_identity=self.store.identity)
            expected = {"jobs": {key: row.model_dump(mode="json")
                                 for key, row in self._jobs.items()}}
            if snapshot.sequence != self._sequence or snapshot.payload != expected:
                raise JobError("job_receipt_changed_outside_owner")
            return {"sequence": snapshot.sequence, "payload_sha256": snapshot.payload_sha256}

    def progress_receipts(self) -> tuple[JobRecord, ...]:
        """Bounded state only; no source inspection or worker takeover authority."""
        with self._lock:
            return tuple(row.record for row in self._jobs.values())

    def run_synchronously(self, call: ToolCall) -> ToolResult:
        """The legacy tool shape, backed by the same preparation/accounting/worker engine."""
        record = self.start(call, mode="synchronous")
        job_id = record.identity.job_id
        until = self._deadlines[job_id] + 20
        self._workers[job_id].join(max(0, until - time.monotonic()))
        with self._lock:
            current = self._entry(job_id)
            if self._workers[job_id].is_alive() or current.result is None:
                self._cancels[job_id].set()
                self._faulted = True
                raise JobError("synchronous_test_outcome_unknown")
            return current.result.model_copy(deep=True)

    def cancel(self, job_id: str) -> JobRecord:
        with self._lock:
            entry = self._entry(job_id)
            if entry.record.status in TERMINAL_STATUSES:
                return entry.record
            self._cancels[job_id].set()
            if entry.record.status == JobStatus.CANCELLING:
                return entry.record
            record = transition(entry.record, JobStatus.CANCELLING,
                                process_started=entry.record.process_started)
            self._persist(entry.model_copy(update={"record": record}))
            return record

    def read(self, job_id: str, *, stream: str = "stdout", offset: int = 0,
             limit: int = 4096) -> dict:
        with self._lock:
            entry = self._entry(job_id)
            capture = self._captures.get(job_id)
            if capture is not None:
                return {"job_id": job_id, **capture.read(stream=stream, offset=offset, limit=limit)}
            refs = tuple(ref for ref in self.artifacts.refs()
                         if ref.call_id == entry.record.identity.call_id
                         and ref.tool_name == "run_tests")
            if len(refs) == 1:
                page = self.artifacts.read(refs[0].artifact_id, stream=stream,
                                           offset=offset, limit=limit)
                return {"job_id": job_id, "final": True, **page.model_dump(mode="json")}
            return {"job_id": job_id, **self.artifacts.read_capture(
                entry.capture_id, stream=stream, offset=offset, limit=limit)}

    def shutdown(self, timeout: float = 20.0) -> bool:
        with self._lock:
            self._accepting = False
            for event in self._cancels.values():
                event.set()
            workers = tuple(self._workers.values())
        until = time.monotonic() + timeout
        for worker in workers:
            if worker.ident is not None:
                worker.join(max(0, until - time.monotonic()))
        return (not any(worker.is_alive() for worker in workers)
                and not self.active and not self.has_unknown)
