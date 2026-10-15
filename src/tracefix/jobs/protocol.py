"""Fixed worker tickets: the host owns the budget; an executor can only report observations."""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, model_validator

from tracefix.jobs.constants import JOB_PROTOCOL_VERSION
from tracefix.jobs.domain import TERMINAL_STATUSES, JobError, JobRecord, JobStatus
from tracefix.jobs.executions import TestExecution
from tracefix.messages import ToolCall
from tracefix.tools.base import ToolResult


class WorkerStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    worker_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    logical_run_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    job_id: str = Field(pattern=r"^job_[a-f0-9]{32}$")
    ticket: TestExecution
    call_json: str = Field(min_length=1, max_length=64 * 1024)
    deadline: AwareDatetime
    mode: Literal["background", "synchronous"] = "background"

    @property
    def call(self) -> ToolCall:
        # Mutable ToolCall/arguments instances cannot alter a validated worker request.
        return ToolCall.model_validate_json(self.call_json)

    @model_validator(mode="before")
    @classmethod
    def integer_protocol_version(cls, value):
        if isinstance(value, dict) and "version" in value and type(value["version"]) is not int:
            raise ValueError("job protocol version must be an integer")
        return value

    @model_validator(mode="after")
    def bound_dispatched_invocation(self) -> WorkerStartRequest:
        if (self.call.name != "run_tests" or self.call.id != self.ticket.call_id
                or self.ticket.phase != "dispatched" or self.ticket.process_started is not None
                or self.ticket.tree_exit_confirmed
                or hashlib.sha256(self.call.model_dump_json().encode()).hexdigest()
                != self.ticket.invocation_sha256):
            raise ValueError("worker requires the exact host-dispatched pytest invocation")
        return self


class WorkerTicketMirror:
    """No limit, counter or budget allocation: one host ticket is armed per control call."""

    def __init__(self, *, run_id: str, worker_epoch: str, host_owner_epoch: str) -> None:
        self.run_id, self.worker_epoch = run_id, worker_epoch
        self.host_owner_epoch = host_owner_epoch
        self._lock = threading.RLock()
        self._rows: dict[str, TestExecution] = {}
        self._armed: str | None = None
        self._dispatched: set[str] = set()

    @property
    def has_unknown(self) -> bool:
        with self._lock:
            return any(row.phase == "unknown" for row in self._rows.values())

    def arm(self, request: WorkerStartRequest) -> None:
        with self._lock:
            request = WorkerStartRequest.model_validate_json(request.model_dump_json())
            if (request.logical_run_id != self.run_id or request.worker_epoch != self.worker_epoch
                    or request.ticket.owner_epoch != self.host_owner_epoch):
                raise JobError("worker_ticket_epoch_or_run_mismatch")
            if self._armed is not None or self.has_unknown:
                raise JobError("worker_ticket_pending_or_unknown")
            if (request.ticket.execution_id in self._rows or any(
                row.call_id == request.call.id for row in self._rows.values()
            )):
                raise JobError("worker_ticket_replay_refused")
            if len(self._rows) >= 4096:
                raise JobError("worker_ticket_receipt_limit")
            self._rows[request.ticket.execution_id] = request.ticket
            self._armed = request.ticket.execution_id

    def reject_unconsumed(self) -> TestExecution:
        """Controller-only: preparation rejected the call before service.reserve was reached."""
        with self._lock:
            if self._armed is None:
                raise JobError("worker_ticket_already_consumed")
            row = self.get(self._armed)
            self._armed = None
            return self.settle(row.execution_id, process_started=False, tree_exit_confirmed=True)

    def reserve(self, *, call_id: str, invocation_sha256: str) -> TestExecution:
        with self._lock:
            if self._armed is None:
                raise JobError("worker_requires_host_ticket")
            row = self.get(self._armed)
            if row.call_id != call_id or row.invocation_sha256 != invocation_sha256:
                raise JobError("worker_ticket_invocation_mismatch")
            self._armed = None
            return row

    def get(self, execution_id: str) -> TestExecution:
        with self._lock:
            if execution_id not in self._rows:
                raise JobError("worker_ticket_not_available")
            return self._rows[execution_id]

    def before_dispatch(self, execution_id: str) -> TestExecution:
        with self._lock:
            row = self.get(execution_id)
            if (row.phase != "dispatched" or row.process_started is not None
                    or execution_id in self._dispatched):
                raise JobError("worker_dispatch_replay_refused")
            if self._armed == execution_id:
                raise JobError("worker_ticket_not_consumed")
            self._dispatched.add(execution_id)
            return row

    def acknowledge_started(self, execution_id: str) -> TestExecution:
        with self._lock:
            row = self.get(execution_id)
            if row.phase not in {"dispatched", "unknown", "settled"}:
                raise JobError("worker_start_observation_invalid")
            updated = row.model_copy(update={"process_started": True})
            self._rows[execution_id] = updated
            return updated

    def settle(self, execution_id: str, *, process_started: bool | None,
               tree_exit_confirmed: bool, outcome_known: bool = True) -> TestExecution:
        with self._lock:
            row = self.get(execution_id)
            if row.process_started is True and process_started is not True:
                raise JobError("worker_start_observation_cannot_be_retracted")
            phase = "unknown"
            if tree_exit_confirmed and outcome_known and process_started is not None:
                phase = "settled" if process_started else "not_started"
            updated = TestExecution.model_validate({
                **row.model_dump(), "phase": phase, "process_started": process_started,
                "tree_exit_confirmed": tree_exit_confirmed,
            })
            if row.phase in {"settled", "not_started", "unknown"} and row != updated:
                raise JobError("worker_terminal_observation_conflict")
            self._rows[execution_id] = updated
            return updated


class WorkerJobReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    logical_run_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    execution: TestExecution
    job: JobRecord | None = None
    result_json: str | None = Field(default=None, max_length=8 * 1024 * 1024)
    error_code: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
                                   max_length=128)

    @property
    def result(self) -> ToolResult | None:
        return (ToolResult.model_validate_json(self.result_json)
                if self.result_json is not None else None)

    @model_validator(mode="after")
    def matching_observations(self) -> WorkerJobReceipt:
        if self.job is not None:
            self.job.require_owner(self.logical_run_id)
            if (self.job.identity.execution_id != self.execution.execution_id
                    or self.job.identity.call_id != self.execution.call_id
                    or self.job.identity.invocation_sha256 != self.execution.invocation_sha256):
                raise ValueError("worker job/execution receipt identity mismatch")
            if (self.job.process_started is True and self.execution.process_started is not True
                    or self.job.status in TERMINAL_STATUSES
                    and self.job.status != JobStatus.OUTCOME_UNKNOWN
                    and (self.execution.phase not in {"settled", "not_started"}
                         or self.job.process_started is not self.execution.process_started)):
                raise ValueError("worker job/execution observations conflict")
        elif self.execution.phase == "settled":
            raise ValueError("settled worker execution omitted its job")
        result = self.result
        if result is not None:
            if (self.job is None or result.call_id != self.execution.call_id
                    or result.tool_name != "run_tests"):
                raise ValueError("worker result has no matching execution")
            encoded = json.dumps(result.model_dump(mode="json"), ensure_ascii=False,
                                 sort_keys=True, separators=(",", ":")).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            if digest != self.job.outcome_sha256:
                raise ValueError("worker outcome digest mismatch")
        elif self.job is not None and self.job.outcome_sha256 is not None:
            raise ValueError("worker receipt omitted a committed outcome")
        return self


class WorkerJobQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    job_id: str = Field(pattern=r"^job_[a-f0-9]{32}$")


class WorkerReadQuery(WorkerJobQuery):
    stream: Literal["stdout", "stderr"] = "stdout"
    offset: StrictInt = Field(default=0, ge=0, le=8 * 1024 * 1024)
    limit: StrictInt = Field(default=4096, ge=1, le=4096)


class WorkerShutdownRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")


class JobWorkerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_run_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    worker_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    host_owner_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    session_deadline: AwareDatetime


class JobWorkerContext(JobWorkerSettings):
    contract: Literal["tracefix-docker-control-v1"]
    physical_run_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    fixed_control_tokens: tuple[tuple[StrictInt, str], ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def initial_kernel_roles(self) -> JobWorkerContext:
        tokens = self.fixed_control_tokens
        if (tokens[0][0] != 1 or tokens[1][0] <= 1
                or any(not start.isdecimal() for _, start in tokens)):
            raise ValueError("job context must bind initial init and idle kernel identities")
        return self


class JobProtocolHello(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: StrictInt = Field(ge=JOB_PROTOCOL_VERSION, le=JOB_PROTOCOL_VERSION)
    worker_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    logical_run_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    host_owner_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    context_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
