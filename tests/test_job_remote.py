from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tracefix.jobs.domain import JobError, JobRecord, JobStatus
from tracefix.jobs.protocol import JobWorkerSettings, WorkerJobReceipt, WorkerStartRequest
from tracefix.jobs.remote import RemoteJobService, _digest
from tracefix.jobs.test_budget import TestExecutionLedger
from tracefix.messages import ToolCall
from tracefix.tools.base import ToolResult


class Channel:
    def __init__(self):
        self.calls, self.finish, self.no_launch, self.fault = [], False, False, None
        self.receipt = None

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if self.fault == "transport":
            raise TimeoutError("worker reply lost")
        if name == "__job_start__":
            request = WorkerStartRequest.model_validate(arguments)
            execution = request.ticket.model_copy(update={
                "phase": "not_started" if self.no_launch else "dispatched",
                "process_started": not self.no_launch,
                "tree_exit_confirmed": self.no_launch,
            })
            record = None if self.no_launch else JobRecord(
                identity={"job_id": request.job_id, "run_id": request.logical_run_id,
                          "call_id": execution.call_id, "execution_id": execution.execution_id,
                          "invocation_sha256": execution.invocation_sha256},
                revision=2, status=JobStatus.RUNNING, process_started=True,
                created_at=datetime.now(UTC), deadline=request.deadline)
            self.receipt = WorkerJobReceipt(worker_epoch=request.worker_epoch,
                logical_run_id=request.logical_run_id, execution=execution, job=record)
        elif name == "__job_status__" and self.finish:
            row = self.receipt
            result = ToolResult(call_id=row.execution.call_id, tool_name="run_tests",
                                success=False, error="test process cancelled")
            record = JobRecord.model_validate({
                **row.job.model_dump(), "revision": row.job.revision + 1,
                "status": JobStatus.CANCELLED, "tree_exit_confirmed": True,
                "outcome_sha256": _digest(result)})
            self.receipt = row.model_copy(update={"job": record,
                "result_json": result.model_dump_json(), "execution": row.execution.model_copy(
                    update={"phase": "settled", "tree_exit_confirmed": True})})
        elif name == "__job_shutdown__":
            return ToolResult(call_id="control", tool_name=name, success=True, output={
                "closed": self.finish or self.no_launch, "active": not self.finish
                and not self.no_launch, "outcome_unknown": False})
        elif name == "__job_read__":
            return ToolResult(call_id="control", tool_name=name, success=True, output={
                "job_id": arguments["job_id"], "content": "middle", "next_offset": 6})
        payload = self.receipt.model_dump(mode="json")
        if self.fault == "epoch":
            payload["worker_epoch"] = "f" * 32
        elif self.fault == "outcome":
            payload["result_json"] = ToolResult(
                call_id="forged", tool_name="run_tests", success=True).model_dump_json()
        return ToolResult(call_id="control", tool_name=name, success=True, output=payload)


def setup(tmp_path, *, limit=2, ledger=None, channel=None, authorize=lambda call: None):
    ledger = ledger or TestExecutionLedger(tmp_path, run_id="root-run", limit=limit)
    settings = JobWorkerSettings(logical_run_id="root-run", worker_epoch=uuid4().hex,
        host_owner_epoch=ledger.owner_epoch,
        session_deadline=datetime.now(UTC) + timedelta(seconds=20))
    history, abort = [], []
    channel = channel or Channel()
    service = RemoteJobService(tmp_path / "jobs", settings=settings, checkout_id="b" * 64,
        ledger=ledger, channel=channel, prepare=lambda call: 10, authorize=authorize,
        historical_read=lambda record, **kwargs: history.append(record.identity.job_id)
        or {"content": "restored artifact", "final": True},
        abort_executor=lambda: abort.append(True))
    return service, ledger, channel, history, abort


def test_root_receipts_poll_settle_once_and_cancel_is_not_completion(tmp_path):
    service, ledger, channel, _, abort = setup(tmp_path)
    with ledger:
        call = ToolCall(id="one", name="run_tests", arguments={"command": "pytest -q"})
        record = service.start(call)
        assert record.status == JobStatus.RUNNING and service.active
        assert ledger.charged == 1 and ledger.held == 0
        assert service.cancel(record.identity.job_id).status == JobStatus.RUNNING
        assert service.read(record.identity.job_id)["content"] == "middle"
        channel.finish = True
        final = service.get(record.identity.job_id)
        assert final.status == JobStatus.CANCELLED and final.tree_exit_confirmed
        calls = len(channel.calls)
        assert service.cancel(final.identity.job_id) == final
        assert service.get(final.identity.job_id) == final and len(channel.calls) == calls
        assert len(service.background_completions()) == 1
        assert service.checkpoint_identity()["sequence"] > 1
        assert service.shutdown() and not abort and ledger.charged == 1


@pytest.mark.parametrize("fault", ["transport", "epoch", "outcome"])
def test_lost_or_forged_start_receipt_is_unknown_and_never_replayed(tmp_path, fault):
    service, ledger, channel, _, abort = setup(tmp_path)
    with ledger:
        channel.fault = fault
        record = service.start(ToolCall(id="one", name="run_tests", arguments={}))
        assert record.status == JobStatus.OUTCOME_UNKNOWN and service.has_unknown
        assert ledger.held == 1 and ledger.charged == 0
        before = len(channel.calls)
        assert service.get(record.identity.job_id) == record
        assert service.cancel(record.identity.job_id) == record
        with pytest.raises(JobError, match="unknown"):
            service.start(ToolCall(id="two", name="run_tests", arguments={}))
        assert len(channel.calls) == before
        assert not service.shutdown(timeout=0) and abort == [True]
        assert ledger.held == 1


def test_known_no_launch_releases_only_root_reservation(tmp_path):
    service, ledger, channel, _, abort = setup(tmp_path)
    with ledger:
        channel.no_launch = True
        record = service.start(ToolCall(id="one", name="run_tests", arguments={}))
        assert record.status == JobStatus.FAILED_TO_START and record.tree_exit_confirmed
        assert ledger.charged == ledger.held == 0
        assert not service.result(record.identity.job_id).success
        assert service.shutdown() and not abort


def test_recovery_uses_history_reader_never_old_worker_or_replay(tmp_path):
    service, ledger, channel, _, _ = setup(tmp_path)
    call = ToolCall(id="one", name="run_tests", arguments={})
    record = service.start(call)
    channel.finish = True
    service.get(record.identity.job_id)
    identity = service.checkpoint_identity()
    ledger.close()
    restored, ledger, channel, history, _ = setup(tmp_path)
    with ledger:
        assert restored.checkpoint_identity() == identity
        assert restored.get(record.identity.job_id).status == JobStatus.CANCELLED
        assert restored.read(record.identity.job_id)["content"] == "restored artifact"
        assert history == [record.identity.job_id] and not channel.calls
        assert ledger.charged == 1
        with pytest.raises(JobError, match="replay_refused"):
            restored.start(call)
        assert not channel.calls


def test_crash_pending_remote_job_is_unknown_without_contacting_new_worker(tmp_path):
    service, ledger, channel, _, _ = setup(tmp_path)
    record = service.start(ToolCall(id="one", name="run_tests", arguments={}))
    ledger.close()
    restored, ledger, channel, _, abort = setup(tmp_path)
    with ledger:
        assert restored.get(record.identity.job_id).status == JobStatus.OUTCOME_UNKNOWN
        assert restored.has_unknown and ledger.charged == 1
        assert not channel.calls
        assert not restored.shutdown(timeout=0) and abort == [True]
        assert all(name != "__job_start__" for name, _ in channel.calls)


def test_denial_bad_prepare_and_cross_run_access_have_zero_reservations(tmp_path):
    def deny(call):
        raise JobError("policy_denied")

    service, ledger, channel, _, _ = setup(tmp_path, authorize=deny)
    with ledger:
        call = ToolCall(id="one", name="run_tests", arguments={})
        initial = ledger.checkpoint_identity()
        with pytest.raises(JobError, match="policy_denied"):
            service.start(call)
        service.authorize = lambda call: None

        def bad_prepare(call):
            raise JobError("parameters_invalid")

        service.prepare = bad_prepare
        with pytest.raises(JobError, match="parameters_invalid"):
            service.start(call)
        for method in (service.get, service.cancel, service.read):
            with pytest.raises(JobError, match="not_available"):
                method("job_" + "f" * 32)
        assert not channel.calls and ledger.checkpoint_identity() == initial


def test_receipt_mutation_after_commit_fails_checkpoint_and_recovery(tmp_path):
    service, ledger, channel, _, _ = setup(tmp_path)
    record = service.start(ToolCall(id="one", name="run_tests", arguments={}))
    channel.finish = True
    service.get(record.identity.job_id)
    saved = service.store.load(current_identity=service.store.identity)
    payload = json.loads(json.dumps(saved.payload))
    row = payload["jobs"][record.identity.job_id]
    row["call_json"] = ToolCall(id="forged", name="run_tests", arguments={}).model_dump_json()
    service.store.save(payload, sequence=saved.sequence + 1)
    with pytest.raises(JobError, match="outside_owner"):
        service.checkpoint_identity()
    ledger.close()
    reopened = TestExecutionLedger(tmp_path, run_id="root-run", limit=2)
    try:
        with pytest.raises(ValueError, match="provenance"):
            setup(tmp_path, ledger=reopened)
    finally:
        reopened.close()
