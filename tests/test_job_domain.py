from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from tracefix.jobs.domain import (
    JobError,
    JobIdentity,
    JobRecord,
    JobStatus,
    select_deadline,
    transition,
)


@pytest.mark.parametrize("changes", [
    {"created_at": datetime(2026, 10, 16)},
    {"deadline": datetime(2026, 10, 15, tzinfo=UTC)},
    {"tree_exit_confirmed": True},
    {"status": "running", "process_started": False},
    {"status": "starting", "process_started": None, "tree_exit_confirmed": True},
    {"status": "running", "process_started": True, "outcome_sha256": "f" * 64},
])
def test_persisted_state_cannot_claim_impossible_observations(changes):
    value = record().model_dump()
    value.update(changes)
    with pytest.raises(ValidationError):
        JobRecord.model_validate(value)


def record() -> JobRecord:
    now = datetime(2026, 10, 16, tzinfo=UTC)
    return JobRecord(
        identity=JobIdentity(job_id="job_" + "a" * 32, run_id="run-1", call_id="call:1",
                             execution_id="b" * 32, invocation_sha256="c" * 64),
        created_at=now, deadline=now + timedelta(seconds=30),
    )


def running() -> JobRecord:
    starting = transition(record(), JobStatus.STARTING, process_started=None)
    return transition(starting, JobStatus.RUNNING, process_started=True)


@pytest.mark.parametrize("field,value", [
    ("revision", True), ("revision", "1"), ("schema_version", True),
    ("process_started", "false"), ("tree_exit_confirmed", 1),
])
def test_persisted_observations_cannot_coerce_untrusted_types(field, value):
    payload = record().model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        JobRecord.model_validate(payload)


def test_completed_process_requires_receipt_and_is_idempotent():
    current = running()
    with pytest.raises(ValidationError, match="outcome receipt"):
        transition(current, JobStatus.COMPLETED, process_started=True, tree_exit_confirmed=True)
    completed = transition(current, JobStatus.COMPLETED, process_started=True,
                           tree_exit_confirmed=True, outcome_sha256="d" * 64)
    assert completed.revision == 3
    assert transition(completed, JobStatus.COMPLETED, process_started=True,
                      tree_exit_confirmed=True, outcome_sha256="d" * 64) is completed
    with pytest.raises(JobError, match="conflict"):
        transition(completed, JobStatus.COMPLETED, process_started=True,
                   tree_exit_confirmed=True, outcome_sha256="e" * 64)


def test_cancel_request_is_not_cancellation_and_late_start_is_not_refunded():
    starting = transition(record(), JobStatus.STARTING, process_started=None)
    pending = transition(starting, JobStatus.CANCELLING, process_started=None)
    acknowledged = transition(pending, JobStatus.CANCELLING, process_started=True)
    assert not acknowledged.tree_exit_confirmed
    with pytest.raises(JobError, match="cannot_be_retracted"):
        transition(acknowledged, JobStatus.CANCELLED, process_started=False,
                   tree_exit_confirmed=True)
    with pytest.raises(ValidationError, match="tree cleanup"):
        transition(acknowledged, JobStatus.CANCELLED, process_started=True)
    cancelled = transition(acknowledged, JobStatus.CANCELLED, process_started=True,
                           tree_exit_confirmed=True)
    assert cancelled.process_started


@pytest.mark.parametrize("terminal", [JobStatus.CANCELLED, JobStatus.DEADLINE_EXCEEDED])
def test_confirmed_non_execution_can_be_closed_without_test_run(terminal):
    closed = transition(record(), terminal, process_started=False, tree_exit_confirmed=True)
    assert closed.process_started is False
    with pytest.raises(JobError, match="not_allowed"):
        transition(closed, JobStatus.STARTING, process_started=None)


def test_failed_start_requires_confirmation_and_unknown_cannot_be_replayed():
    starting = transition(record(), JobStatus.STARTING, process_started=None)
    with pytest.raises(ValidationError, match="non-execution"):
        transition(starting, JobStatus.FAILED_TO_START, process_started=None)
    unknown = transition(starting, JobStatus.OUTCOME_UNKNOWN, process_started=None)
    with pytest.raises(JobError, match="not_allowed"):
        transition(unknown, JobStatus.STARTING, process_started=None)
    with pytest.raises(JobError, match="not_allowed"):
        transition(unknown, JobStatus.COMPLETED, process_started=True,
                   tree_exit_confirmed=True, outcome_sha256="e" * 64)
    failed = transition(starting, JobStatus.FAILED_TO_START, process_started=False,
                        tree_exit_confirmed=True)
    assert failed.process_started is False


def test_natural_completion_can_win_cancel_race_and_unknown_keeps_cleanup_axis():
    cancelling = transition(running(), JobStatus.CANCELLING, process_started=True)
    completed = transition(cancelling, JobStatus.COMPLETED, process_started=True,
                           tree_exit_confirmed=True, outcome_sha256="d" * 64)
    with pytest.raises(JobError, match="not_allowed"):
        transition(completed, JobStatus.CANCELLED, process_started=True,
                   tree_exit_confirmed=True)
    unknown = transition(running(), JobStatus.OUTCOME_UNKNOWN, process_started=True,
                         tree_exit_confirmed=True)
    assert unknown.tree_exit_confirmed  # A lost result is not the same as a live process.


def test_owner_and_opaque_identity_reject_cross_run_paths_and_extra_fields():
    item = record()
    item.require_owner("run-1")
    with pytest.raises(JobError, match="owner_mismatch"):
        item.require_owner("other")
    for changes in ({"job_id": "../../other"}, {"run_id": "../run"}, {"pid": 123}):
        with pytest.raises(ValidationError):
            JobIdentity.model_validate({**item.identity.model_dump(), **changes})


def test_deadline_selects_earliest_request_host_and_session_limit():
    now = record().created_at
    chosen = select_deadline(now=now, timeout_seconds=None, default_timeout_seconds=120,
                             session_deadline=now + timedelta(seconds=90))
    assert chosen == now + timedelta(seconds=90)
    assert select_deadline(now=now, timeout_seconds=30, default_timeout_seconds=120,
                           session_deadline=now + timedelta(seconds=90),
                           host_deadline=now + timedelta(seconds=10)) == now + timedelta(seconds=10)


@pytest.mark.parametrize("timeout", [0, -1, 601, float("nan"), float("inf"), True])
def test_invalid_timeout_is_rejected_without_process_or_budget(timeout):
    now = record().created_at
    with pytest.raises(JobError, match="timeout_invalid"):
        select_deadline(now=now, timeout_seconds=timeout, default_timeout_seconds=120,
                        session_deadline=now + timedelta(seconds=90))


def test_naive_and_expired_deadlines_are_rejected():
    now = record().created_at
    with pytest.raises(JobError, match="timezone_missing"):
        select_deadline(now=now.replace(tzinfo=None), timeout_seconds=1,
                        default_timeout_seconds=120, session_deadline=now)
    with pytest.raises(JobError, match="already_expired"):
        select_deadline(now=now, timeout_seconds=1, default_timeout_seconds=120,
                        session_deadline=now)
    with pytest.raises(ValidationError, match="timezone"):
        JobRecord.model_validate({**record().model_dump(), "created_at": now.replace(tzinfo=None)})
