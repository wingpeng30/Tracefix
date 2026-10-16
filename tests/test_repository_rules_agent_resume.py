import copy
import json
from types import SimpleNamespace

import pytest

from tracefix import (
    AgentConfig,
    BaseLLM,
    LLMConfig,
    LLMResponse,
    Message,
    MessageRole,
    MinimalAgent,
    TokenUsage,
    ToolCall,
)
from tracefix.exceptions import AgentError
from tracefix.memory_domain import issue_user_input_receipt
from tracefix.repository_rules.access import ToolAccessPlanner
from tracefix.repository_rules.delivery import RuleDeliveryLedger
from tracefix.repository_rules.domain import RuleDocument, RuleSelection, RuleSourceVersion, digest
from tracefix.repository_rules.gate import RuleAccessGate
from tracefix.repository_rules.search import PagedRepositorySearch
from tracefix.task_state import TaskStateError, TaskStateSession
from tracefix.tools.base import ToolRegistry
from tracefix.tools.repository_search import RepositorySearchTool


class Model(BaseLLM):
    def __init__(self, steps):
        super().__init__(LLMConfig(model_name="zero-provider"))
        self.steps, self.requests = iter(steps), []

    def complete(self, messages, tools=()):
        self.requests.append(tuple(messages))
        calls = next(self.steps)
        return LLMResponse(model_name="zero-provider", usage=TokenUsage(cost_usd=0),
            message=Message(role=MessageRole.ASSISTANT, tool_calls=calls,
                            content=None if calls else "done"))


def query(call_id, cursor=None):
    return ToolCall(id=call_id, name="search_code", arguments={"query": "needle", "max_results": 1,
                    **({"cursor": cursor} if cursor is not None else {})})


def assemble(model, run="run-a"):
    ledger, reads = RuleDeliveryLedger(), []

    def select(target):
        doc = RuleDocument.create(kind="ancestor", source_path="AGENTS.md", body=b"Required rule")
        return RuleSelection.create(target, (doc,), (
            RuleSourceVersion(source_path="AGENTS.md", sha256=doc.body_sha256),))

    def read(path):
        reads.append(path)
        return b"needle first\nneedle second"

    selector = SimpleNamespace(select=select)
    engine = PagedRepositorySearch(run, candidates=lambda args: ("a.py",),
        source=SimpleNamespace(read_bytes=read), selector=selector, delivery=ledger)
    tool = RepositorySearchTool(engine)
    agent = MinimalAgent(model, ToolRegistry((tool,)), AgentConfig(max_steps=8))
    agent.rule_access_gate = RuleAccessGate(ToolAccessPlanner(), selector, ledger,
                                            search_validator=tool, run_id=run)
    return agent, ledger, engine, reads


def paused_state():
    agent, ledger, engine, reads = assemble(Model([(query("initial"),)]))
    captured = []

    def pause(active):
        snapshot = active.checkpoint_payload()
        captured.append(snapshot)
        if active.state.step_count == 1:
            raise KeyboardInterrupt

    agent.checkpoint_callback = pause
    with pytest.raises(KeyboardInterrupt):
        agent.run("search under repository rules")
    assert len(captured) == 2 and reads == []
    value = json.loads(json.dumps(captured[-1]))
    output = next(json.loads(message["content"])["output"] for message in value["history"]
                  if message["role"] == "tool")
    return value, output["cursor"]


def test_safe_batch_checkpoint_restores_rules_and_cursor_into_a_fresh_agent():
    saved, cursor = paused_state()
    model = Model([(query("resumed", cursor),), ()])
    agent, ledger, engine, reads = assemble(model)
    state = agent.resume(saved)
    assert state.status.value == "completed" and state.step_count == 3
    assert state.search_calls == 2 and state.test_runs == 0
    assert reads == ["a.py"]
    assert any(message.metadata.get("kind") == "repository_rules" for message in model.requests[0])
    # One old pending cursor plus the new in-file continuation are retained.
    current = agent.checkpoint_payload()
    cursors = current["repository_rules"]["payload"]["search"]["payload"]["cursors"]
    assert len(cursors) == 2 and cursor in cursors


@pytest.mark.parametrize("mode", ["disabled", "missing", "run", "catalog", "cursor"])
def test_invalid_or_incompatible_rules_restore_is_rejected_before_model_call(mode):
    saved, cursor = paused_state()
    model = Model([])
    agent, ledger, engine, reads = assemble(model, run="other" if mode == "run" else "run-a")
    if mode == "disabled":
        agent.rule_access_gate = None
    elif mode == "missing":
        saved.pop("repository_rules")
    elif mode == "catalog":
        agent.config.context.agent_tool_enabled = True
    elif mode == "cursor":
        rules = saved["repository_rules"]
        search = rules["payload"]["search"]
        search["payload"]["cursors"][cursor]["line_offset"] = 2
        search["payload_sha256"] = digest(search["payload"])
        rules["payload_sha256"] = digest(rules["payload"])
    with pytest.raises(AgentError):
        agent.resume(saved)
    assert model.requests == [] and reads == []


def test_bad_search_component_cannot_partially_publish_delivery_component():
    saved, cursor = paused_state()
    agent, ledger, engine, reads = assemble(Model([]))
    before = agent.rule_access_gate.checkpoint((), agent._request_tool_specs())
    history = tuple(Message.model_validate(message) for message in saved["history"])
    bad = copy.deepcopy(saved["repository_rules"])
    bad["payload"]["search"]["payload"]["cursors"][cursor]["position"] = 42
    search = bad["payload"]["search"]
    search["payload_sha256"] = digest(search["payload"])
    bad["payload_sha256"] = digest(bad["payload"])
    with pytest.raises(ValueError):
        agent.rule_access_gate.restore(bad, history, agent._request_tool_specs())
    assert agent.rule_access_gate.checkpoint((), agent._request_tool_specs()) == before
    assert not reads


def test_task_state_keeps_only_verified_rule_references_and_never_user_requirements():
    task = "search under repository rules"
    receipt = issue_user_input_receipt("direct-user", 1, task).model_dump(mode="json")
    agent, ledger, engine, reads = assemble(Model([(query("initial"),)]))
    agent.task_state_session = TaskStateSession()
    agent.initial_user_receipt = receipt
    saved = []

    def pause(active):
        if active.state.step_count == 1:
            saved.append(active.checkpoint_payload())
            raise KeyboardInterrupt

    agent.checkpoint_callback = pause
    with pytest.raises(KeyboardInterrupt):
        agent.run(task)
    current = agent.task_state_session.snapshot()
    assert len(current.requirements) == 1 and current.requirements[0].text == task
    references = [reference for reference in current.references if reference.kind == "rule"]
    assert len(references) == 1
    assert references[0].reference_id in ledger.registered_message_ids
    assert "Required rule" not in current.model_dump_json()
    assert any("rules pending" in observation.summary for observation in current.observations)
    history = tuple(Message.model_validate(value) for value in saved[0]["history"])
    # A USER role and repository_rules marker alone cannot mint a trusted reference.
    with pytest.raises(TaskStateError, match="committed rule state"):
        TaskStateSession().restore(saved[0]["task_state"], history,
                                    trusted_receipts={"direct-user": receipt})
    resumed, *_ = assemble(Model([()]))
    resumed.task_state_session = TaskStateSession()
    state = resumed.resume(saved[0], trusted_user_receipts={"direct-user": receipt})
    assert state.status.value == "completed"
    assert any(reference.kind == "rule"
               for reference in resumed.task_state_session.snapshot().references)
