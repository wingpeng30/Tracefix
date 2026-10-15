from __future__ import annotations

import hashlib
import json
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
from tracefix.jobs.ports import JobSourceState
from tracefix.jobs.process_tree import ProcessStartError, ProcessTreeScope, WindowsJobScope
from tracefix.jobs.service import JobService
from tracefix.jobs.session import LocalAgentJobSession
from tracefix.jobs.test_budget import TestExecutionLedger as Ledger
from tracefix.messages import ToolCall
from tracefix.test_execution import PreparedTest
from tracefix.tools.base import ToolResult
from tracefix.tools.builtin import RunTestsTool
from tracefix.tools.jobs import ManagedRunTestsTool


@pytest.mark.skipif(os.name != "nt", reason="Windows native managed agent test")
def test_agent_projects_one_budget_and_namespaces_reused_provider_call_ids(tmp_path):
    from tracefix import AgentConfig, BaseLLM, LLMConfig, MinimalAgent
    from tracefix.jobs.local_runtime import LocalJobRuntime
    from tracefix.messages import Message, MessageRole
    from tracefix.models.base import LLMResponse, TokenUsage
    from tracefix.tools.base import ToolRegistry

    class Model(BaseLLM):
        def __init__(self):
            super().__init__(LLMConfig(model_name="zero-provider-managed-tests"))
            self.requests = 0

        def complete(self, messages, tools=()):
            self.requests += 1
            message = (Message(role=MessageRole.ASSISTANT, tool_calls=(
                ToolCall(id="reused-provider-id", name="run_tests", arguments={}),))
                if self.requests <= 2 else Message(role=MessageRole.ASSISTANT, content="done"))
            return LLMResponse(message=message, usage=TokenUsage(
                input_tokens=1, output_tokens=1, total_tokens=2, cost_usd=0),
                model_name="zero-provider-managed-tests")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "test_one.py").write_text("def test_one():\n    assert True\n", encoding="utf-8")
    for arguments in [("init", "-q"), ("add", "."),
                      ("-c", "user.name=TraceFix Tests", "-c", "user.email=test@example.com",
                       "commit", "-qm", "baseline")]:
        subprocess.run(["git", *arguments], cwd=workspace, check=True, capture_output=True)
    store = ArtifactStore(tmp_path / "artifacts", run_id="r1", streaming_enabled=True)
    tool = RunTestsTool(workspace, evidence_dir=tmp_path / "audit", artifact_store=store)
    runtime = LocalJobRuntime(
        tools=ToolRegistry([tool]), artifacts=store, run_dir=tmp_path, run_id="r1",
        checkout_id="a" * 64, limit=2, deadline=datetime.now(UTC) + timedelta(seconds=30),
        profile="workspace_write", source_identity=lambda: JobSourceState(
            product_diff_sha256=tool._product_diff_sha(), nonempty_diff=False))
    try:
        agent = MinimalAgent(Model(), runtime.tools,
                             config=AgentConfig(max_steps=3, max_test_runs=2))
        agent.tool_dispatcher, agent.job_session = runtime.dispatcher, runtime.session
        state = agent.run("run the same valid suite twice")
        assert state.test_runs == runtime.ledger.charged == 2 and runtime.ledger.held == 0
        calls = [call for message in agent.history.snapshot() for call in message.tool_calls]
        assert len(calls) == 2 and calls[0].id != calls[1].id
        results = [message for message in agent.history.snapshot()
                   if message.role is MessageRole.TOOL]
        assert len(results) == 2
        assert all('"test_status":"passed"' in message.content for message in results)
        runtime.checkpoints.verify_saved(runtime.checkpoints.safe_identity())
    finally:
        assert runtime.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native background agent test")
def test_agent_reads_middle_cancels_and_projects_versioned_job_reference(tmp_path):
    from tracefix import AgentConfig, BaseLLM, LLMConfig, MinimalAgent
    from tracefix.capabilities import CapabilityCatalog, CapabilityPolicy, ToolDispatcher
    from tracefix.jobs.policy import JobActivityGuard
    from tracefix.messages import Message, MessageRole
    from tracefix.models.base import LLMResponse, TokenUsage
    from tracefix.task_state import TaskStateSession
    from tracefix.tools import create_default_tool_registry
    from tracefix.tools.base import ToolRegistry
    from tracefix.tools.jobs import register_job_tools

    class Model(BaseLLM):
        def __init__(self):
            super().__init__(LLMConfig(model_name="zero-provider-background-agent"))
            self.job_id, self.cursor = None, None
            self.stage = "start"
            self.middle_seen = False

        def complete(self, messages, tools=()):
            time.sleep(0.03)
            outputs = []
            for message in messages:
                if message.role is MessageRole.TOOL:
                    payload = json.loads(message.content)
                    if payload.get("success") and isinstance(payload.get("output"), dict):
                        outputs.append(payload)
            if self.job_id is None:
                for payload in outputs:
                    if payload.get("tool_name") == "start_test_job":
                        self.job_id = payload["output"]["job"]["identity"]["job_id"]
            if self.stage == "start":
                calls = (ToolCall(id="start", name="start_test_job", arguments={
                    "command": "pytest -q -s test_one.py::test_long"}),
                    ToolCall(id="read", name="read_file", arguments={"path": "test_one.py"}))
                self.stage = "read"
            elif self.stage == "read":
                pages = [p["output"] for p in outputs if p.get("tool_name") == "read_job_output"]
                if pages:
                    self.cursor = pages[-1]["cursor"]
                    self.middle_seen |= "MIDDLE-KEY" in pages[-1]["content"]
                if self.middle_seen:
                    calls = (ToolCall(id="cancel", name="cancel_job",
                                      arguments={"job_id": self.job_id}),)
                    self.stage = "wait"
                else:
                    args = {"job_id": self.job_id, "limit": 4000}
                    if self.cursor is not None:
                        args["cursor"] = self.cursor
                    calls = (ToolCall(id="page", name="read_job_output", arguments=args),)
            elif self.stage == "wait":
                states = [p["output"]["job"]["status"] for p in outputs
                          if p.get("tool_name") in {"get_job_status", "cancel_job"}]
                if states and states[-1] == "cancelled":
                    calls = (ToolCall(id="short", name="run_tests", arguments={
                        "command": "pytest -q test_one.py::test_short"}),)
                    self.stage = "finish"
                else:
                    calls = (ToolCall(id="status", name="get_job_status",
                                      arguments={"job_id": self.job_id}),)
            else:
                calls = ()
            return LLMResponse(message=Message(role=MessageRole.ASSISTANT, tool_calls=calls,
                                               content=None if calls else "done"),
                               usage=TokenUsage(input_tokens=1, output_tokens=1,
                                                total_tokens=2, cost_usd=0),
                               model_name="zero-provider-background-agent")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "test_one.py").write_text(
        "import os,time\ndef test_long():\n"
        "    os.write(1,b'x'*50000+b'MIDDLE-KEY'+b'y'*50000+b'\\n')\n"
        "    time.sleep(60)\ndef test_short():\n    assert True\n", encoding="utf-8")
    for arguments in [("init", "-q"), ("add", "."),
                      ("-c", "user.name=TraceFix Tests", "-c", "user.email=test@example.com",
                       "commit", "-qm", "baseline")]:
        subprocess.run(["git", *arguments], cwd=workspace, check=True, capture_output=True)
    store = ArtifactStore(tmp_path / "artifacts", run_id="r1", streaming_enabled=True)
    originals = create_default_tool_registry(workspace, evidence_dir=tmp_path / "audit",
                                               artifact_store=store)
    tool = originals.get("run_tests")
    with Ledger(tmp_path, run_id="r1", limit=2) as ledger:
        service = JobService(
            tmp_path / "jobs", run_id="r1", checkout_id="a" * 64, ledger=ledger,
            artifact_store=store, test_tool=tool, authorize=lambda call: None,
            scope_factory=WindowsJobScope, evaluate=tool.evaluate_job_result,
            verify_source=tool.verify_prepared_source,
            session_deadline=datetime.now(UTC) + timedelta(seconds=15),
        )
        registry = ToolRegistry(ManagedRunTestsTool(tool, service) if item.spec.name == "run_tests"
                                else item for item in originals)
        register_job_tools(registry, service)
        model = Model()
        agent = MinimalAgent(model, registry, config=AgentConfig(max_steps=60, max_test_runs=2))
        agent.tool_dispatcher = ToolDispatcher(
            CapabilityPolicy(CapabilityCatalog.for_tool_names(registry.names), "workspace_write"),
            execution_guard=JobActivityGuard(service))

        def source_state():
            assert not service.active, "main-thread source helper must wait for test quiescence"
            return JobSourceState(product_diff_sha256=tool._product_diff_sha(), nonempty_diff=False)

        agent.job_session = LocalAgentJobSession(service, ledger, source_state)
        agent.task_state_session = TaskStateSession()
        agent.task_state_source_identity = lambda: source_state().product_diff_sha256
        task = "read the long test middle, cancel it, and run the short test"
        agent.initial_user_receipt = {"issuer": "tracefix-host-v1", "message_id": "user-job",
                                      "turn": 1,
                                      "content_sha256": hashlib.sha256(task.encode()).hexdigest()}
        try:
            state = agent.run(task)
            assert model.middle_seen and ledger.charged == state.test_runs == 2
            assert ledger.held == 0 and not service.has_unknown
            assert not agent.history.pending_tool_call_ids
            refs = agent.task_state_session.snapshot().references
            assert any(ref.kind == "job" and ref.reference_id == model.job_id for ref in refs)
            saved = agent.checkpoint_payload()
            assert saved["jobs"]["consumed"][model.job_id]
        finally:
            assert service.shutdown()


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


@pytest.mark.parametrize("observed_start,cancel_before_start,cancel_during_start", [
    (False, False, False), (None, False, False), (False, True, False), (False, False, True),
])
def test_failed_launch_and_lost_ack_have_different_budget_outcomes(
    tmp_path, observed_start, cancel_before_start, cancel_during_start,
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
            if cancel_during_start:
                ready.set()
                assert release.wait(timeout=3)
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
        root_deadline = datetime.now(UTC) + timedelta(seconds=3)
        job = service.start(ToolCall(id="call1", name="run_tests", arguments={}),
                            host_deadline=root_deadline, assigned_job_id="job_" + "f" * 32)
        assert job.deadline == root_deadline and job.identity.job_id == "job_" + "f" * 32
        if cancel_before_start or cancel_during_start:
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
        expected = (JobStatus.CANCELLED if cancel_before_start or cancel_during_start
                    else JobStatus.FAILED_TO_START
                    if observed_start is False else JobStatus.OUTCOME_UNKNOWN)
        assert service.get(job.identity.job_id).status == expected
        assert ledger.charged == 0 and ledger.held == (observed_start is None)
        assert bool(dispatched) is not cancel_before_start
        if observed_start is False:
            before = ledger.checkpoint_identity()
            with pytest.raises(JobError, match="job_id_replay"):
                service.start(ToolCall(id="new-call", name="run_tests", arguments={}),
                              assigned_job_id=job.identity.job_id)
            assert ledger.checkpoint_identity() == before
        assert service.shutdown() is (observed_start is False)


@pytest.mark.parametrize("failure_stage", ["dispatch_intent", "source_check"])
def test_prelaunch_failure_releases_budget_and_seals_capture(tmp_path, monkeypatch, failure_stage):
    launches, closures = [], []

    class Recipe:
        def prepare(self, call):
            return [sys.executable, "-m", "pytest"], 5

        def prepare_test(self, call):
            return PreparedTest(call.model_dump_json(), (sys.executable, "-m", "pytest"), 5,
                                (), tmp_path, tmp_path, "a" * 32, tmp_path / "audit",
                                tmp_path / "junit", "b" * 64, time.monotonic())

    class Scope(ProcessTreeScope):
        def _attach(self, process):
            raise AssertionError("must not attach")

        def start(self, *args, **kwargs):
            launches.append(True)
            raise AssertionError("must not launch")

        def terminate_tree(self):
            raise AssertionError("must not terminate an unstarted process")

        def wait_empty(self, timeout=3):
            return True

        def close(self):
            closures.append(True)
            return True

    def fail(*args):
        raise JobError("injected_prelaunch_failure")

    store = ArtifactStore(tmp_path / "artifacts", run_id="r1", streaming_enabled=True)
    with Ledger(tmp_path, run_id="r1", limit=1) as ledger:
        if failure_stage == "dispatch_intent":
            monkeypatch.setattr(ledger, "before_dispatch", fail)
        service = JobService(
            tmp_path / "jobs", run_id="r1", checkout_id="a" * 64, ledger=ledger,
            artifact_store=store, test_tool=Recipe(), authorize=lambda call: None,
            scope_factory=Scope, verify_source=fail if failure_stage == "source_check" else
            lambda prepared: None,
            evaluate=lambda prepared, capture: ToolResult(
                call_id="call1", tool_name="run_tests", success=False, error=capture.process_error),
            session_deadline=datetime.now(UTC) + timedelta(seconds=10),
        )
        call = ToolCall(id="call1", name="run_tests", arguments={})
        if failure_stage == "dispatch_intent":
            with pytest.raises(JobError, match="injected_prelaunch_failure"):
                service.start(call)
        else:
            service.start(call)
        until = time.monotonic() + 5
        while service.active and time.monotonic() < until:
            time.sleep(0.01)
        rows = service.background_completions()
        assert len(rows) == 1
        record, outcome = rows[0]
        assert record.status == JobStatus.FAILED_TO_START
        assert record.process_started is False and record.tree_exit_confirmed
        assert not outcome.success and outcome.error == "JobError"
        assert ledger.charged == ledger.held == 0
        assert not launches and closures and not service.has_unknown
        page = service.read(record.identity.job_id)
        assert page["final"] and page["content"] == ""
        assert len(store.refs()) == 1
        assert service.shutdown()


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
            synchronous = ManagedRunTestsTool(test_tool, service)
            assert synchronous.spec == test_tool.spec
            outcome = synchronous.execute(ToolCall(id="second", name="run_tests", arguments={}))
            second = service.get(outcome.metadata["managed_job_id"])
            assert second.status == JobStatus.COMPLETED
            assert outcome.output["test_status"] == "passed"
            assert service.result(second.identity.job_id) == outcome
            assert [record.identity.job_id for record, _ in service.background_completions()] == [
                first.identity.job_id]
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
