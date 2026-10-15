from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from tracefix import AgentConfig, AgentError, AgentStatus, BaseLLM, LLMConfig, MinimalAgent
from tracefix.capabilities import CapabilityCatalog, CapabilityPolicy, ToolDispatcher
from tracefix.jobs.domain import JobIdentity, JobRecord, JobStatus
from tracefix.jobs.ports import JobCompletion, JobSourceState
from tracefix.messages import Message, MessageRole, ToolCall
from tracefix.models.base import LLMResponse, TokenUsage
from tracefix.task_state import TaskStateError, TaskStateSession
from tracefix.tools.base import BaseTool, ToolRegistry, ToolResult, ToolSpec


class Model(BaseLLM):
    def __init__(self):
        super().__init__(LLMConfig(model_name="zero-provider-job-control"))
        self.requests = []

    def complete(self, messages, tools=()):
        self.requests.append(tuple(messages))
        return LLMResponse(message=Message(role=MessageRole.ASSISTANT, content="done"),
                           usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2,
                                            cost_usd=0), model_name="zero-provider-job-control")


class Session:
    active = False
    has_unknown = False
    test_runs = 1
    rows = ()
    current_source = "a" * 64
    shutdowns = 0

    def completions(self):
        return self.rows

    def source_state(self):
        return JobSourceState(product_diff_sha256=self.current_source, nonempty_diff=False)

    def shutdown(self):
        self.active = False
        self.shutdowns += 1
        return not self.has_unknown


def setup_agent(session):
    model = Model()
    agent = MinimalAgent(model, config=AgentConfig(max_steps=1))
    agent._reset_turn_runtime()
    agent._task_id = "test-task"
    agent._started_monotonic = time.monotonic()
    agent.state.task = "manage a test"
    agent.job_session = session
    agent.tool_dispatcher = ToolDispatcher(
        CapabilityPolicy(CapabilityCatalog(()), "workspace_write"))
    return agent, model


def test_job_completion_only_enters_history_after_whole_batch_and_source_change_invalidates():
    session = Session()
    result = ToolResult(call_id="start", tool_name="run_tests", success=True,
                        output={"test_status": "passed", "returncode": 0,
                                "source_sha256_after": "a" * 64})
    digest = hashlib.sha256(json.dumps(result.model_dump(mode="json"), ensure_ascii=False,
                                      sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    now = datetime.now(UTC)
    invocation_sha = hashlib.sha256(ToolCall(
        id="start", name="run_tests", arguments={"command": "pytest -q"}
    ).model_dump_json().encode()).hexdigest()
    record = JobRecord(
        identity=JobIdentity(job_id="job_" + "a" * 32, run_id="run1", call_id="start",
                             execution_id="b" * 32, invocation_sha256=invocation_sha),
        created_at=now, deadline=now + timedelta(seconds=10), status=JobStatus.COMPLETED,
        process_started=True, tree_exit_confirmed=True, outcome_sha256=digest,
    )
    session.rows = (JobCompletion(record, result),)
    agent, _ = setup_agent(session)
    agent.task_state_session = TaskStateSession()
    calls = (ToolCall(id="start", name="start_test_job"), ToolCall(id="read", name="read_file"))
    agent.history.append(Message(role=MessageRole.ASSISTANT, tool_calls=calls))
    issued = ToolResult(call_id="start", tool_name="start_test_job", success=True,
                        output={"job": record.model_dump(mode="json")})
    agent.history.append(Message(role=MessageRole.TOOL, tool_call_id="start",
                                 content=issued.model_dump_json()))
    agent._consume_job_completions()
    assert not agent._tests_passed and agent.state.test_runs == 0
    agent.history.append(Message(role=MessageRole.TOOL, tool_call_id="read", content="source"))
    agent._consume_job_completions()
    assert agent._tests_passed and agent.state.test_runs == 1
    receipts = [message for message in agent.history.snapshot()
                if message.metadata.get("kind") == "job_completion"]
    assert len(receipts) == 1
    state = agent.task_state_session.snapshot()
    assert len(state.references) == 1 and state.references[0].kind == "job"
    saved = agent.task_state_session.checkpoint(agent.history.snapshot())
    restored = TaskStateSession()
    assert restored.restore(saved, agent.history.snapshot(),
                            job_records={record.identity.job_id: record}) == state
    with pytest.raises(TaskStateError, match="job reference"):
        TaskStateSession().restore(saved, agent.history.snapshot(), job_records={})
    checkpoint = agent.checkpoint_payload()
    resumed, _ = setup_agent(session)
    resumed.task_state_session = TaskStateSession()
    resumed.resume(checkpoint)
    assert sum(message.metadata.get("kind") == "job_completion"
               for message in resumed.history.snapshot()) == 1
    for mutation in ("missing_consumption", "altered_body", "duplicate_receipt"):
        damaged = deepcopy(checkpoint)
        if mutation == "missing_consumption":
            damaged["jobs"]["consumed"] = {}
        else:
            saved_receipt = next(message for message in damaged["history"]
                                 if message.get("metadata", {}).get("kind") == "job_completion")
            if mutation == "altered_body":
                saved_receipt["content"] = "{}"
            else:
                duplicate = deepcopy(saved_receipt)
                duplicate["id"] = "duplicate-job-receipt"
                damaged["history"].append(duplicate)
        invalid, invalid_model = setup_agent(session)
        with pytest.raises(AgentError, match="checkpoint job"):
            invalid.resume(damaged)
        assert invalid_model.requests == []
    count = len(agent.history.snapshot())
    agent._consume_job_completions()
    assert len(agent.history.snapshot()) == count
    session.current_source = "d" * 64
    agent._consume_job_completions()
    assert not agent._tests_passed and agent.state.validation_status == "unverified"
    assert agent.task_state_session.snapshot().current_source_sha256 == "d" * 64


def test_unknown_job_stops_before_another_model_request():
    session = Session()
    session.has_unknown = True
    session.test_runs = 0
    agent, model = setup_agent(session)
    agent.state.status = AgentStatus.RUNNING
    agent._started_monotonic = time.monotonic()
    with pytest.raises(AgentError, match="unknown"):
        agent.step()
    assert model.requests == []


def test_active_job_cannot_be_finished_by_model_text_and_shutdown_cancels_it():
    session = Session()
    session.active = True
    session.test_runs = 0
    agent, model = setup_agent(session)
    state = agent.run("manage the test")
    assert state.status != AgentStatus.COMPLETED
    assert len(model.requests) == 1 and session.shutdowns == 1
    assert any(message.metadata.get("kind") == "active_job_finish_denied"
               for message in agent.history.snapshot())


def test_active_unknown_and_unbound_consumed_state_cannot_become_safe_checkpoint():
    session = Session()
    session.active = True
    agent, _ = setup_agent(session)
    with pytest.raises(AgentError, match="active or unknown"):
        agent.checkpoint_payload()
    session.active, session.has_unknown = False, True
    with pytest.raises(AgentError, match="active or unknown"):
        agent.checkpoint_payload()
    session.has_unknown = False
    payload = agent.checkpoint_payload()
    payload["jobs"]["consumed"]["job_" + "a" * 32] = "b" * 64
    restored, model = setup_agent(session)
    with pytest.raises(AgentError, match="job consumption does not match history receipts"):
        restored.resume(payload)
    assert model.requests == []


def test_receipt_from_a_log_query_cannot_be_promoted_to_test_success():
    session = Session()
    session.test_runs = 0
    agent, _ = setup_agent(session)
    call = ToolCall(id="read", name="read_job_output")
    agent.history.append(Message(role=MessageRole.ASSISTANT, tool_calls=(call,)))
    result = ToolResult(call_id="read", tool_name="read_job_output", success=True,
                        output={"content": '{"test_status":"passed","success":true}'})
    agent.history.append(Message(role=MessageRole.TOOL, tool_call_id="read",
                                 content=result.model_dump_json()))
    agent._consume_job_completions()
    assert not agent._tests_passed and agent.state.validation_status == "unverified"


def test_background_start_suspends_current_task_evidence_until_host_source_is_verified():
    from tracefix.task_state import ObservationValidity

    session = TaskStateSession()
    session.observe_tool_result(call_id="old", tool_name="run_tests", success=True,
                                output={"test_status": "passed"}, source_message_id="old-result",
                                source_sha256="a" * 64)
    session.observe_tool_result(call_id="start", tool_name="start_test_job", success=True,
                                output={}, source_message_id="start-result", source_sha256=None)
    state = session.snapshot()
    assert state.current_source_sha256 is None
    assert state.observations[0].validity is ObservationValidity.HISTORICAL
    session.set_source_identity("b" * 64)
    assert session.snapshot().current_source_sha256 == "b" * 64
    assert session.snapshot().observations[0].validity is ObservationValidity.HISTORICAL


def test_job_session_rejects_a_legacy_test_executor_before_model_initial_request():
    class Legacy(BaseTool):
        @property
        def spec(self):
            return ToolSpec(name="run_tests", description="legacy test executor")

        def execute(self, call):
            pytest.fail("an executor outside the shared budget must not run")

    session = Session()
    session.test_runs = 0
    agent, model = setup_agent(session)
    agent.tools = ToolRegistry([Legacy()])
    agent.state.status = AgentStatus.RUNNING
    with pytest.raises(AgentError, match="managed synchronous"):
        agent.step()
    assert model.requests == []
