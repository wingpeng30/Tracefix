"""Pure job identities, lifecycle transitions and bounded deadline selection."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


class JobError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class JobStatus(StrEnum):
    NOT_STARTED = "not_started"
    STARTING = "starting"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED_TO_START = "failed_to_start"
    CANCELLED = "cancelled"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    OUTCOME_UNKNOWN = "outcome_unknown"


TERMINAL_STATUSES = frozenset({
    JobStatus.COMPLETED, JobStatus.FAILED_TO_START, JobStatus.CANCELLED,
    JobStatus.DEADLINE_EXCEEDED, JobStatus.OUTCOME_UNKNOWN,
})
_TRANSITIONS = {
    JobStatus.NOT_STARTED: frozenset({
        JobStatus.STARTING, JobStatus.CANCELLED, JobStatus.DEADLINE_EXCEEDED,
    }),
    JobStatus.STARTING: frozenset({
        JobStatus.RUNNING, JobStatus.CANCELLING, JobStatus.FAILED_TO_START,
        JobStatus.CANCELLED, JobStatus.DEADLINE_EXCEEDED, JobStatus.OUTCOME_UNKNOWN,
    }),
    JobStatus.RUNNING: frozenset({
        JobStatus.CANCELLING, JobStatus.COMPLETED, JobStatus.CANCELLED,
        JobStatus.DEADLINE_EXCEEDED, JobStatus.OUTCOME_UNKNOWN,
    }),
    JobStatus.CANCELLING: frozenset({
        JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.DEADLINE_EXCEEDED,
        JobStatus.OUTCOME_UNKNOWN,
    }),
}


class JobIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(pattern=r"^job_[a-f0-9]{32}$")
    run_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    call_id: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:-]+$")
    execution_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    invocation_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


class JobRecord(BaseModel):
    """Outcome and process cleanup are independent; neither implies test success."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    identity: JobIdentity
    revision: StrictInt = Field(default=0, ge=0)
    status: JobStatus = JobStatus.NOT_STARTED
    created_at: datetime
    deadline: datetime
    process_started: StrictBool | None = False
    tree_exit_confirmed: StrictBool = False
    outcome_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def consistent(self) -> JobRecord:
        if not _aware(self.created_at) or not _aware(self.deadline):
            raise ValueError("job timestamps must include a timezone")
        if self.deadline <= self.created_at:
            raise ValueError("job deadline must follow creation")
        if self.status == JobStatus.NOT_STARTED:
            if self.process_started is not False or self.tree_exit_confirmed:
                raise ValueError("not-started job cannot claim process observations")
        elif self.status == JobStatus.RUNNING:
            if self.process_started is not True or self.tree_exit_confirmed:
                raise ValueError("running job requires a live confirmed process")
        elif self.status == JobStatus.COMPLETED:
            if (self.process_started is not True or not self.tree_exit_confirmed
                    or self.outcome_sha256 is None):
                raise ValueError("completed job requires process exit and an outcome receipt")
        elif self.status == JobStatus.FAILED_TO_START:
            if self.process_started is not False or not self.tree_exit_confirmed:
                raise ValueError("failed-to-start requires confirmed non-execution")
        elif self.status in {JobStatus.CANCELLED, JobStatus.DEADLINE_EXCEEDED}:
            if self.process_started is None or not self.tree_exit_confirmed:
                raise ValueError("cancelled or expired job requires confirmed tree cleanup")
        elif self.status in {JobStatus.STARTING, JobStatus.CANCELLING}:
            if self.tree_exit_confirmed:
                raise ValueError("an in-flight job cannot claim tree cleanup")
        if self.outcome_sha256 is not None and self.status not in TERMINAL_STATUSES:
            raise ValueError("in-flight job cannot publish a final outcome")
        return self

    def require_owner(self, run_id: str) -> None:
        if self.identity.run_id != run_id:
            raise JobError("job_owner_mismatch")


def transition(
    record: JobRecord,
    status: JobStatus,
    *,
    process_started: bool | None,
    tree_exit_confirmed: bool = False,
    outcome_sha256: str | None = None,
) -> JobRecord:
    """Require explicit observations, preserve start accounting, reject stale rewrites."""
    status = JobStatus(status)
    values = {
        **record.model_dump(), "status": status,
        "process_started": process_started, "tree_exit_confirmed": tree_exit_confirmed,
        "outcome_sha256": outcome_sha256,
    }
    if record.process_started is True and process_started is not True:
        raise JobError("job_start_observation_cannot_be_retracted")
    if status == record.status:
        if values == record.model_dump():
            return record
        # A delayed start acknowledgement may arrive after cancellation was requested.
        if not (status == JobStatus.CANCELLING and record.process_started is None
                and process_started is True):
            raise JobError("job_transition_conflict")
    elif status not in _TRANSITIONS.get(record.status, frozenset()):
        raise JobError("job_transition_not_allowed")
    values["revision"] = record.revision + 1
    return JobRecord.model_validate(values)


def select_deadline(
    *, now: datetime, timeout_seconds: float | None,
    default_timeout_seconds: float, session_deadline: datetime,
    host_deadline: datetime | None = None,
) -> datetime:
    """Choose once; status polling and cancellation cannot extend this deadline."""
    if any(not _aware(value) for value in (now, session_deadline)):
        raise JobError("job_deadline_timezone_missing")
    if host_deadline is not None and not _aware(host_deadline):
        raise JobError("job_deadline_timezone_missing")
    requested = default_timeout_seconds if timeout_seconds is None else timeout_seconds
    if isinstance(requested, bool) or not math.isfinite(requested) or not 0 < requested <= 600:
        raise JobError("job_timeout_invalid")
    deadline = min(now + timedelta(seconds=requested), session_deadline)
    if host_deadline is not None:
        deadline = min(deadline, host_deadline)
    if deadline <= now:
        raise JobError("job_deadline_already_expired")
    return deadline
