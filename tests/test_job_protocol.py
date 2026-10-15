from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tracefix.jobs.domain import JobError
from tracefix.jobs.protocol import WorkerStartRequest, WorkerTicketMirror
from tracefix.jobs.test_budget import TestExecution as ExecutionRow
from tracefix.messages import ToolCall


def request(**changes):
    call = ToolCall(id="call1", name="run_tests", arguments={"command": "pytest -q"})
    ticket = ExecutionRow(execution_id="a" * 32, call_id=call.id,
                           invocation_sha256=hashlib.sha256(call.model_dump_json().encode()).hexdigest(),
                           owner_epoch="b" * 32, phase="dispatched", process_started=None)
    return WorkerStartRequest.model_validate({
        "version": 1, "worker_epoch": "c" * 32, "logical_run_id": "root-run",
        "job_id": "job_" + "d" * 32,
        "ticket": ticket, "call_json": call.model_dump_json(),
        "deadline": datetime.now(UTC), **changes})


def mirror():
    return WorkerTicketMirror(run_id="root-run", worker_epoch="c" * 32, host_owner_epoch="b" * 32)


def test_worker_has_no_budget_authority_and_cannot_execute_without_an_exact_host_ticket():
    worker, start = mirror(), request()
    assert not any(hasattr(worker, name) for name in ("limit", "charged", "held"))
    with pytest.raises(JobError, match="requires_host_ticket"):
        worker.reserve(call_id=start.call.id, invocation_sha256=start.ticket.invocation_sha256)
    worker.arm(start)
    with pytest.raises(JobError, match="not_consumed"):
        worker.before_dispatch(start.ticket.execution_id)
    with pytest.raises(JobError, match="invocation_mismatch"):
        worker.reserve(call_id="forged", invocation_sha256=start.ticket.invocation_sha256)
    ticket = worker.reserve(call_id=start.call.id,
                            invocation_sha256=start.ticket.invocation_sha256)
    assert ticket == start.ticket
    assert worker.before_dispatch(ticket.execution_id) == ticket
    with pytest.raises(JobError, match="replay"):
        worker.before_dispatch(ticket.execution_id)
    worker.acknowledge_started(ticket.execution_id)
    with pytest.raises(JobError, match="replay"):
        worker.before_dispatch(ticket.execution_id)
    with pytest.raises(JobError, match="cannot_be_retracted"):
        worker.settle(ticket.execution_id, process_started=False, tree_exit_confirmed=True)
    terminal = worker.settle(ticket.execution_id, process_started=True, tree_exit_confirmed=True)
    assert terminal.phase == "settled"
    assert worker.settle(ticket.execution_id, process_started=True,
                          tree_exit_confirmed=True) == terminal
    with pytest.raises(JobError, match="replay"):
        worker.arm(start)
    with pytest.raises(JobError, match="terminal_observation_conflict"):
        worker.settle(ticket.execution_id, process_started=True, tree_exit_confirmed=False)


@pytest.mark.parametrize("field,value", [
    ("logical_run_id", "other-run"), ("worker_epoch", "e" * 32),
])
def test_other_namespace_and_old_worker_epoch_are_rejected(field, value):
    with pytest.raises(JobError, match="epoch_or_run_mismatch"):
        mirror().arm(request(**{field: value}))


def test_wrong_host_epoch_and_unexpected_schema_do_not_arm_the_worker():
    start = request()
    wrong = start.model_copy(update={"ticket": start.ticket.model_copy(
        update={"owner_epoch": "e" * 32})})
    with pytest.raises(JobError, match="epoch_or_run_mismatch"):
        mirror().arm(wrong)
    for change in ({"version": 2}, {"version": True}, {"origin": "host"},
                   {"call_json": ToolCall(id="call1", name="apply_patch").model_dump_json()},
                   {"deadline": datetime.now()}):
        with pytest.raises(ValidationError):
            request(**change)


def test_only_unconsumed_preparation_failures_have_a_definite_no_launch_receipt():
    worker, start = mirror(), request()
    worker.arm(start)
    with pytest.raises(JobError, match="pending"):
        worker.arm(start)
    assert worker.reject_unconsumed().phase == "not_started"
    with pytest.raises(JobError, match="already_consumed"):
        worker.reject_unconsumed()
    worker = mirror()
    worker.arm(start)
    row = worker.reserve(call_id=start.call.id, invocation_sha256=start.ticket.invocation_sha256)
    with pytest.raises(JobError, match="already_consumed"):
        worker.reject_unconsumed()
    unknown = worker.settle(row.execution_id, process_started=None,
                            tree_exit_confirmed=True, outcome_known=False)
    assert unknown.phase == "unknown" and worker.has_unknown
    observed = worker.acknowledge_started(row.execution_id)
    assert observed.phase == "unknown" and observed.process_started is True and worker.has_unknown
    with pytest.raises(JobError, match="pending_or_unknown"):
        worker.arm(start)


def test_request_detaches_mutable_arguments_and_arm_revalidates_copied_models():
    start = request()
    changed = start.call
    changed.arguments["command"] = "not pytest"
    assert start.call.arguments["command"] == "pytest -q"
    corrupt = start.model_copy(update={"call_json": changed.model_dump_json()})
    worker = mirror()
    with pytest.raises(ValidationError, match="exact host-dispatched"):
        worker.arm(corrupt)
    worker.arm(start)
