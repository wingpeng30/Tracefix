from __future__ import annotations

import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from tracefix.artifacts import ArtifactStore
from tracefix.jobs.capture import LiveCaptureResult, LiveProcessCapture
from tracefix.jobs.domain import JobError, JobStatus
from tracefix.jobs.process_tree import ProcessTreeScope
from tracefix.jobs.service import JobService
from tracefix.jobs.test_budget import TestExecutionLedger
from tracefix.messages import ToolCall
from tracefix.test_execution import PreparedTest
from tracefix.tools.base import ToolResult


class Recipe:
    def __init__(self, root):
        self.root = root

    def prepare(self, call):
        return [sys.executable, "-m", "pytest"], 5

    def prepare_test(self, call):
        return PreparedTest(call.model_dump_json(), (sys.executable, "-m", "pytest"), 5,
                            (), self.root, self.root, "a" * 32, self.root / "audit",
                            self.root / "junit", "b" * 64, time.monotonic())


class OwnedDomain(ProcessTreeScope):
    def _attach(self, process):
        raise AssertionError("fault contract must not launch a native process")

    def terminate_tree(self):
        pass

    def wait_empty(self, timeout=3):
        return True

    def close(self):
        self._closed = self._cleaned = True
        return True


def service_for(root, ledger, store, *, evaluate=None):
    return JobService(
        root / "jobs", run_id="r1", checkout_id="a" * 64, ledger=ledger,
        artifact_store=store, test_tool=Recipe(root), authorize=lambda call: None,
        scope_factory=OwnedDomain, verify_source=lambda prepared: None,
        evaluate=evaluate or (lambda prepared, capture: ToolResult(
            call_id="start", tool_name="run_tests", success=False,
            error="synthetic fault contract")),
        session_deadline=datetime.now(UTC) + timedelta(seconds=10))


@pytest.mark.parametrize("fault", ["start_ack", "job_start_receipt", "evaluate", "final_receipt"])
def test_postdispatch_fault_retains_charge_and_refuses_replay_across_owner_epochs(
    tmp_path, monkeypatch, fault,
):
    executed = []

    def observed_execution(self, *args, scope, on_started, **kwargs):
        # Synthetic execution observation exercises durable ordering; it grants no test pass.
        executed.append(True)
        scope.process_started = True
        on_started()
        return LiveCaptureResult("", "", 0, True, True, False, False, False, None, None, None)

    def fail(*args, **kwargs):
        raise OSError("injected persistence or evaluation failure")

    monkeypatch.setattr(LiveProcessCapture, "run", observed_execution)
    store = ArtifactStore(tmp_path / "artifacts", run_id="r1", streaming_enabled=True)
    with TestExecutionLedger(tmp_path, run_id="r1", limit=2) as ledger:
        service = service_for(tmp_path, ledger, store,
                              evaluate=fail if fault == "evaluate" else None)
        if fault == "start_ack":
            monkeypatch.setattr(ledger, "acknowledge_started", fail)
        if fault in {"job_start_receipt", "final_receipt"}:
            original = service._persist

            def interrupted_receipt(entry):
                blocked = JobStatus.RUNNING if fault == "job_start_receipt" else JobStatus.COMPLETED
                if entry.record.status == blocked:
                    fail()
                return original(entry)

            monkeypatch.setattr(service, "_persist", interrupted_receipt)
        job = service.start(ToolCall(id="start", name="run_tests", arguments={}))
        until = time.monotonic() + 3
        while service.active and time.monotonic() < until:
            time.sleep(0.005)
        unknown = service.get(job.identity.job_id)
        assert unknown.status == JobStatus.OUTCOME_UNKNOWN
        assert unknown.process_started is True and unknown.tree_exit_confirmed
        assert ledger.charged == 1 and ledger.held == 0
        assert service.has_unknown and service.background_completions() == ()
        with pytest.raises(JobError, match="stopped_or_unknown"):
            service.start(ToolCall(id="retry", name="run_tests", arguments={}))
        with pytest.raises(JobError, match="known_quiescence"):
            service.checkpoint_identity()
        assert not service.shutdown()
    with TestExecutionLedger(tmp_path, run_id="r1", limit=2) as recovered:
        service = service_for(tmp_path, recovered, store)
        assert recovered.charged == 1 and service.has_unknown
        assert service.get(job.identity.job_id).status == JobStatus.OUTCOME_UNKNOWN
        with pytest.raises(JobError, match="stopped_or_unknown"):
            service.start(ToolCall(id="retry", name="run_tests", arguments={}))
        assert executed == [True]
        assert not service.shutdown()


def test_thread_creation_failure_cannot_refund_dispatched_intent(tmp_path, monkeypatch):
    class FailedWorker:
        ident = None

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

        def is_alive(self):
            return False

    store = ArtifactStore(tmp_path / "artifacts", run_id="r1", streaming_enabled=True)
    with TestExecutionLedger(tmp_path, run_id="r1", limit=1) as ledger:
        service = service_for(tmp_path, ledger, store)
        with monkeypatch.context() as patch:
            patch.setattr(threading, "Thread", FailedWorker)
            with pytest.raises(RuntimeError, match="thread unavailable"):
                service.start(ToolCall(id="start", name="run_tests", arguments={}))
        assert ledger.charged == 0 and ledger.held == 1
        assert service.has_unknown and not service.shutdown()
    with TestExecutionLedger(tmp_path, run_id="r1", limit=1) as recovered:
        service = service_for(tmp_path, recovered, store)
        assert recovered.has_unknown and recovered.held == 1
        assert all(row.status == JobStatus.OUTCOME_UNKNOWN for row in service.progress_receipts())
        with pytest.raises(JobError, match="stopped_or_unknown"):
            service.start(ToolCall(id="start", name="run_tests", arguments={}))
        assert not service.shutdown()
