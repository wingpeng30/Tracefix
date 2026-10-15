from __future__ import annotations

import hashlib
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from tracefix.artifacts import ArtifactStore
from tracefix.jobs.domain import JobError, JobStatus
from tracefix.jobs.executions import TestExecution as ExecutionRow
from tracefix.jobs.process_tree import ProcessStartError, ProcessTreeScope
from tracefix.jobs.protocol import (
    WorkerJobQuery,
    WorkerJobReceipt,
    WorkerStartRequest,
    WorkerTicketMirror,
)
from tracefix.jobs.service import JobService
from tracefix.jobs.worker import JobWorkerController
from tracefix.messages import ToolCall
from tracefix.test_execution import PreparedTest
from tracefix.tools.base import ToolResult


def setup_worker(tmp_path, *, observed=False, blocked=False, deny=False, command="pytest -q"):
    ready, release = threading.Event(), threading.Event()
    calls, capabilities = [], []
    call = ToolCall(id="call1", name="run_tests", arguments={"command": command})
    ticket = ExecutionRow(execution_id="a" * 32, call_id=call.id, owner_epoch="b" * 32,
                           phase="dispatched", process_started=None,
                           invocation_sha256=hashlib.sha256(call.model_dump_json().encode()).hexdigest())
    request = WorkerStartRequest(
        version=1, worker_epoch="c" * 32, logical_run_id="root-run", job_id="job_" + "d" * 32,
        ticket=ticket, call_json=call.model_dump_json(),
        deadline=datetime.now(UTC) + timedelta(seconds=5))
    tickets = WorkerTicketMirror(run_id="root-run", worker_epoch="c" * 32,
                                  host_owner_epoch="b" * 32)

    class Recipe:
        def prepare(self, call):
            if call.arguments["command"] != "pytest -q":
                raise JobError("invalid_pytest_command")
            return [sys.executable, "-m", "pytest"], 10

        def prepare_test(self, call):
            return PreparedTest(call.model_dump_json(), (sys.executable, "-m", "pytest"), 10,
                                (), tmp_path, tmp_path, "a" * 32, tmp_path / "audit",
                                tmp_path / "junit", "b" * 64, time.monotonic())

    class Scope(ProcessTreeScope):
        def _attach(self, process):
            raise AssertionError("contract must not start a native process")

        def start(self, *args, **kwargs):
            calls.append(True)
            ready.set()
            if blocked:
                assert release.wait(timeout=3)
            self.process_started = observed
            raise ProcessStartError("controlled_no_start", process_started=observed, cleaned=True)

        def terminate_tree(self):
            pass

        def wait_empty(self, timeout=3):
            return True

        def close(self):
            self._closed = self._cleaned = True
            return True

    def authorize(capability):
        capabilities.append(capability)
        if deny:
            raise JobError("policy_denied")

    artifacts = ArtifactStore(tmp_path / "artifacts", run_id="root-run", streaming_enabled=True)
    service = JobService(
        tmp_path / "jobs", run_id="root-run", checkout_id="e" * 64, ledger=tickets,
        artifact_store=artifacts, test_tool=Recipe(), authorize=lambda call: None,
        scope_factory=Scope, verify_source=lambda prepared: None,
        evaluate=lambda prepared, capture: ToolResult(call_id="call1", tool_name="run_tests",
                                                     success=False, error="controlled no start"),
        session_deadline=datetime.now(UTC) + timedelta(seconds=15))
    controller = JobWorkerController(service=service, tickets=tickets, authorize=authorize)
    query = WorkerJobQuery(worker_epoch=request.worker_epoch, job_id=request.job_id)
    return controller, request, query, calls, capabilities, ready, release


def test_worker_returns_ticket_and_handles_status_read_cancel_without_waiting(tmp_path, capsys):
    controller, request, query, calls, caps, ready, release = setup_worker(tmp_path, blocked=True)
    try:
        receipt = controller.start(request)
        assert receipt.job.identity.job_id == request.job_id
        assert receipt.job.deadline == request.deadline
        assert ready.wait(timeout=3)
        assert controller.status(query).job.status == JobStatus.STARTING
        assert not controller.read(query)["final"]
        assert controller.cancel(query).job.status == JobStatus.CANCELLING
    finally:
        release.set()
    assert controller.service.shutdown()
    final = controller.status(query)
    assert final.job.status == JobStatus.CANCELLED and final.execution.phase == "not_started"
    assert final.result is not None and not final.result.success
    assert controller.cancel(query).job == final.job
    assert controller.read(query)["final"] and calls == [True]
    assert {"job_start_control", "job_status_control", "job_read_control",
            "job_cancel_control"}.issubset(caps)
    assert capsys.readouterr().out == ""
    corrupted = final.model_dump(mode="json")
    changed = final.result.model_copy(update={"error": "changed"})
    corrupted["result_json"] = changed.model_dump_json()
    with pytest.raises(ValidationError, match="digest mismatch"):
        WorkerJobReceipt.model_validate(corrupted)


def test_worker_policy_and_epoch_fail_before_ticket_adoption_or_output_quota(tmp_path):
    controller, request, query, calls, _, _, _ = setup_worker(tmp_path, deny=True)
    with pytest.raises(JobError, match="policy_denied"):
        controller.start(request)
    with pytest.raises(JobError, match="not_available"):
        controller.tickets.get(request.ticket.execution_id)
    assert not calls and controller.service.artifacts.quota().queries == 0
    controller.authorize = lambda capability: None
    wrong = query.model_copy(update={"worker_epoch": "f" * 32})
    for operation in (controller.status, controller.read, controller.cancel):
        with pytest.raises(JobError, match="epoch_mismatch"):
            operation(wrong)
    assert controller.service.artifacts.quota().queries == 0
    with pytest.raises(JobError, match="epoch_mismatch"):
        controller.shutdown(worker_epoch="f" * 32)
    assert controller.shutdown(worker_epoch=request.worker_epoch)["closed"]


def test_preparation_refusal_has_a_bound_no_launch_receipt(tmp_path):
    controller, request, _, calls, _, _, _ = setup_worker(tmp_path, command="rm")
    result = controller.start(request)
    assert result.error_code == "invalid_pytest_command"
    assert result.job is None and result.result is None and not calls
    assert result.execution.phase == "not_started" and result.execution.process_started is False
    assert result.execution.tree_exit_confirmed
    assert controller.service.artifacts.quota().artifacts == 0


def test_lost_start_ack_remains_unknown_even_after_domain_cleanup(tmp_path):
    controller, request, query, calls, _, _, _ = setup_worker(tmp_path, observed=None)
    controller.start(request)
    assert not controller.service.shutdown()
    receipt = controller.status(query)
    assert receipt.job.status == JobStatus.OUTCOME_UNKNOWN
    assert receipt.execution.phase == "unknown" and receipt.execution.process_started is None
    assert receipt.execution.tree_exit_confirmed and calls == [True]
    closed = controller.shutdown(worker_epoch=request.worker_epoch)
    assert closed == {"closed": False, "active": False, "outcome_unknown": True}
