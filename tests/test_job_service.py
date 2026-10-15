from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from tracefix.artifacts import ArtifactStore
from tracefix.checkpoint import CheckpointStore
from tracefix.jobs.domain import TERMINAL_STATUSES, JobError, JobIdentity, JobRecord, JobStatus
from tracefix.jobs.process_tree import ProcessStartError, ProcessTreeScope, WindowsJobScope
from tracefix.jobs.service import JobService
from tracefix.jobs.test_budget import TestExecutionLedger as Ledger
from tracefix.messages import ToolCall
from tracefix.test_execution import PreparedTest
from tracefix.tools.base import ToolResult
from tracefix.tools.builtin import RunTestsTool


class NoExecution:
    def __init__(self):
        self.calls = []

    def prepare(self, call):
        self.calls.append("validate")
        raise JobError("invalid_pytest_command")

    def prepare_test(self, call):
        self.calls.append("prepare")
        raise AssertionError("must not prepare")


def test_authorization_denial_precedes_preparation_reservation_and_scope(tmp_path):
    tool = NoExecution()
    scopes = []
    store = ArtifactStore(tmp_path / "artifacts", run_id="r1", streaming_enabled=True)
    with Ledger(tmp_path, run_id="r1", limit=1) as ledger:
        def deny(call):
            raise JobError("capability_denied")
        service = JobService(
            tmp_path / "jobs", run_id="r1", checkout_id="a" * 64, ledger=ledger,
            artifact_store=store, test_tool=tool, authorize=deny,
            scope_factory=lambda: scopes.append("scope"), evaluate=lambda *args: None,
            verify_source=lambda prepared: None,
            session_deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
        with pytest.raises(JobError, match="capability_denied"):
            service.start(ToolCall(id="call1", name="run_tests", arguments={"command": "rm"}))
        assert tool.calls == scopes == []
        assert ledger.charged == ledger.held == 0 and store.quota().artifacts == 0
        assert service.shutdown()


def test_invalid_arguments_do_not_reserve_or_prepare(tmp_path):
    tool = NoExecution()
    store = ArtifactStore(tmp_path / "artifacts", run_id="r1", streaming_enabled=True)
    with Ledger(tmp_path, run_id="r1", limit=1) as ledger:
        service = JobService(
            tmp_path / "jobs", run_id="r1", checkout_id="a" * 64, ledger=ledger,
            artifact_store=store, test_tool=tool, authorize=lambda call: None,
            scope_factory=lambda: (_ for _ in ()).throw(AssertionError("no scope")),
            evaluate=lambda *args: None, verify_source=lambda prepared: None,
            session_deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
        with pytest.raises(JobError, match="invalid_pytest_command"):
            service.start(ToolCall(id="call1", name="run_tests", arguments={"command": "rm"}))
        assert tool.calls == ["validate"] and ledger.charged == ledger.held == 0
        assert service.shutdown()


def test_recovery_rejects_job_without_matching_root_execution(tmp_path):
    now = datetime.now(UTC)
    record = JobRecord(
        identity=JobIdentity(job_id="job_" + "a" * 32, run_id="r1", call_id="missing-call",
                             execution_id="b" * 32, invocation_sha256="c" * 64),
        created_at=now, deadline=now + timedelta(seconds=10),
        status=JobStatus.OUTCOME_UNKNOWN, process_started=None,
    )
    jobs = CheckpointStore(tmp_path / "jobs", {
        "kind": "job-receipts-v1", "run_id": "r1", "checkout_id": "a" * 64,
    })
    jobs.save({"jobs": {record.identity.job_id: {
        "record": record.model_dump(mode="json"), "capture_id": "d" * 32, "result": None,
    }}}, sequence=1)
    store = ArtifactStore(tmp_path / "artifacts", run_id="r1", streaming_enabled=True)
    with Ledger(tmp_path, run_id="r1", limit=1) as ledger:
        with pytest.raises(JobError, match="not_found"):
            JobService(
                tmp_path / "jobs", run_id="r1", checkout_id="a" * 64, ledger=ledger,
                artifact_store=store, test_tool=NoExecution(), authorize=lambda call: None,
                scope_factory=lambda: None, evaluate=lambda *args: None,
                verify_source=lambda prepared: None,
                session_deadline=now + timedelta(seconds=10),
            )


@pytest.mark.parametrize("observed_start,cancel_before_start", [(False, False), (None, False),
                                                              (False, True)])
def test_failed_launch_and_lost_ack_have_different_budget_outcomes(
    tmp_path, observed_start, cancel_before_start,
):
    ready, release = threading.Event(), threading.Event()
    dispatched = []

    def verify(prepared):
        if cancel_before_start:
            ready.set()
            assert release.wait(timeout=3)
    class Recipe:
        def prepare(self, call):
            return [sys.executable, "-m", "pytest"], 5

        def prepare_test(self, call):
            return PreparedTest(call.model_dump_json(), (sys.executable, "-m", "pytest"), 5,
                                (), tmp_path, tmp_path, "a" * 32,
                                tmp_path / "audit", tmp_path / "junit", "b" * 64,
                                time.monotonic())

    class Scope(ProcessTreeScope):
        def _attach(self, process):
            raise AssertionError("fault injection does not create a real process")

        def start(self, *args, **kwargs):
            dispatched.append(True)
            self.process_started = observed_start
            raise ProcessStartError("injected_start_failure", process_started=observed_start,
                                    cleaned=True)

        def terminate_tree(self):
            pass

        def wait_empty(self, timeout=3):
            return True

        def close(self):
            self._closed = self._cleaned = True
            return True

    store = ArtifactStore(tmp_path / "artifacts", run_id="r1", streaming_enabled=True)
    with Ledger(tmp_path, run_id="r1", limit=1) as ledger:
        service = JobService(
            tmp_path / "jobs", run_id="r1", checkout_id="a" * 64, ledger=ledger,
            artifact_store=store, test_tool=Recipe(), authorize=lambda call: None,
            scope_factory=Scope, verify_source=verify,
            evaluate=lambda prepared, capture: ToolResult(call_id="call1", tool_name="run_tests",
                                                         success=False, error="injected failure"),
            session_deadline=datetime.now(UTC) + timedelta(seconds=10),
        )
        job = service.start(ToolCall(id="call1", name="run_tests", arguments={}))
        if cancel_before_start:
            try:
                assert ready.wait(timeout=3)
                first_cancel = service.cancel(job.identity.job_id)
                assert service.cancel(job.identity.job_id) == first_cancel
                assert ledger.charged == 0 and ledger.held == 1
            finally:
                release.set()
        until = time.monotonic() + 5
        while service.active and time.monotonic() < until:
            time.sleep(0.01)
        expected = (JobStatus.CANCELLED if cancel_before_start else JobStatus.FAILED_TO_START
                    if observed_start is False else JobStatus.OUTCOME_UNKNOWN)
        assert service.get(job.identity.job_id).status == expected
        assert ledger.charged == 0 and ledger.held == (observed_start is None)
        assert bool(dispatched) is not cancel_before_start
        assert service.shutdown() is (observed_start is False)


@pytest.mark.skipif(os.name != "nt", reason="Windows native job lifecycle")
def test_real_pytest_cancel_then_complete_and_recover_without_reexecution(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "test_example.py"
    target.write_text("import time\ndef test_long():\n    print('running', flush=True)\n"
                      "    time.sleep(60)\n", encoding="utf-8")
    for arguments in [("init", "-q"), ("add", "."),
                      ("-c", "user.name=TraceFix Tests", "-c", "user.email=test@example.com",
                       "commit", "-qm", "baseline")]:
        subprocess.run(["git", *arguments], cwd=workspace, check=True, capture_output=True)
    store = ArtifactStore(tmp_path / "artifacts", run_id="r1", streaming_enabled=True)
    test_tool = RunTestsTool(workspace, evidence_dir=tmp_path / "test-evidence",
                            artifact_store=store)

    def assemble(ledger):
        return JobService(
            tmp_path / "jobs", run_id="r1", checkout_id="a" * 64,
            ledger=ledger, artifact_store=store, test_tool=test_tool, authorize=lambda call: None,
            scope_factory=WindowsJobScope, evaluate=test_tool.evaluate_job_result,
            verify_source=test_tool.verify_prepared_source,
            session_deadline=datetime.now(UTC) + timedelta(seconds=30),
        )

    def wait(service, job_id):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            record = service.get(job_id)
            if record.status in TERMINAL_STATUSES:
                return record
            time.sleep(0.02)
        pytest.fail("native pytest worker did not settle")

    with Ledger(tmp_path, run_id="r1", limit=2) as ledger:
        service = assemble(ledger)
        try:
            first = service.start(ToolCall(id="first", name="run_tests",
                                           arguments={"command": "pytest -q -s"}))
            with pytest.raises(JobError, match="already_active"):
                service.start(ToolCall(id="duplicate", name="run_tests", arguments={}))
            for _ in range(50):
                if "running" in service.read(first.identity.job_id)["content"]:
                    break
                time.sleep(0.03)
            service.cancel(first.identity.job_id)
            assert wait(service, first.identity.job_id).status == JobStatus.CANCELLED
            assert not service.result(first.identity.job_id).success
            target.write_text("def test_short():\n    assert 2 + 2 == 4\n", encoding="utf-8")
            second = service.start(ToolCall(id="second", name="run_tests", arguments={}))
            assert wait(service, second.identity.job_id).status == JobStatus.COMPLETED
            assert service.result(second.identity.job_id).output["test_status"] == "passed"
            assert ledger.charged == 2 and ledger.held == 0
            assert service.cancel(second.identity.job_id).status == JobStatus.COMPLETED
        finally:
            assert service.shutdown()
    with Ledger(tmp_path, run_id="r1", limit=2) as restored:
        service = assemble(restored)
        assert service.get(second.identity.job_id).status == JobStatus.COMPLETED
        assert service.read(first.identity.job_id)["final"]
        assert restored.charged == 2 and not service.active
        assert service.shutdown()
