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
    ToolCall,
)
from tracefix.capabilities import (
    CapabilityCatalog,
    CapabilityPolicy,
    ExecutionProfile,
    ToolDispatcher,
)
from tracefix.repository_rules.access import ToolAccessPlanner
from tracefix.repository_rules.delivery import RuleRequestTicket
from tracefix.repository_rules.domain import RuleDocument, RuleSelection, RuleSourceVersion
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.gate import RuleAccessGate
from tracefix.tools.base import ToolRegistry
from tracefix.tools.builtin import ApplyPatchTool, ReadFileTool, RunTestsTool


def call(name, **arguments):
    return ToolCall(id="gate-contract", name=name, arguments=arguments)


def selection(target):
    doc = RuleDocument.create(kind="ancestor", source_path="AGENTS.md", body=b"Read carefully.")
    return RuleSelection.create(target, (doc,), (
        RuleSourceVersion(source_path="AGENTS.md", sha256=doc.body_sha256),))


class Delivery:
    def __init__(self):
        self.delivered = False
        self.pending = []

    def is_delivered(self, value):
        return self.delivered

    def defer(self, value):
        self.pending.append(value)

    def prepare_request(self, messages, tools=(), *, history_prefix=None):
        return RuleRequestTicket("fake", "0" * 64, ())

    def confirm_response(self, ticket, response_message_id=None):
        pass

    def reject_unsent_request(self, ticket):
        pass

    def close_batch(self, history):
        return ()


def gate(delivery, **kwargs):
    return RuleAccessGate(ToolAccessPlanner(), SimpleNamespace(select=selection),
                          delivery, **kwargs)


def test_pending_does_not_grant_delivery_and_rechecks_on_each_access():
    delivery = Delivery()
    guard = gate(delivery)
    request = call("read_file", path="src/a.py")
    assert guard.check(request).reason_code == "rules_pending"
    assert not guard.check(request).allowed
    assert len(delivery.pending) == 2
    delivery.delivered = True
    assert guard.check(request).allowed
    delivery.delivered = False
    assert not guard.check(request).allowed


def test_every_patch_path_is_checked_before_any_delivery_is_queued():
    delivery = Delivery()
    calls = []

    def select(target):
        calls.append(target)
        if target == "z/b.py":
            raise RuleError("rules_configuration_invalid")
        return selection(target)

    guard = RuleAccessGate(ToolAccessPlanner(), SimpleNamespace(select=select), delivery)
    with pytest.raises(RuleError, match="configuration_invalid"):
        guard.check(call("apply_patch", patch="--- a/a.py\n+++ b/z/b.py"))
    assert calls == ["a.py", "z/b.py"]
    assert delivery.pending == []


@pytest.mark.parametrize("name", ["run_tests", "start_test_job", "search_code", "get_git_diff"])
def test_metadata_enumeration_cannot_be_silently_omitted(name):
    arguments = {"query": "x"} if name == "search_code" else {}
    with pytest.raises(RuleError, match="metadata_resolver_required"):
        gate(Delivery()).check(call(name, **arguments))


def test_test_resolver_never_omits_preparation_configurations():
    delivery = Delivery()
    resolver = SimpleNamespace(resolve=lambda plan: ("tests/a.py",))
    guard = gate(delivery, resolver=resolver)
    result = guard.check(call("run_tests", command="pytest tests"))
    assert not result.allowed
    assert [item.target_path for item in delivery.pending] == [
        "tests/a.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"]


def test_metadata_resolver_cannot_omit_explicit_read_or_patch_paths():
    guard = gate(Delivery(), resolver=SimpleNamespace(resolve=lambda plan: ()))
    for request in (call("read_file", path="a.py"),
                    call("apply_patch", patch="--- a/old.py\n+++ b/new.py")):
        with pytest.raises(RuleError, match="required_path_missing"):
            guard.check(request)


def test_complete_selection_memory_is_bounded_before_queueing(monkeypatch):
    import tracefix.repository_rules.gate as module

    delivery = Delivery()
    monkeypatch.setattr(module, "MAX_ACCESS_SELECTION_BYTES", 1)
    with pytest.raises(RuleError, match="access_selection_limit"):
        gate(delivery).check(call("read_file", path="a.py"))
    assert delivery.pending == []


@pytest.mark.parametrize("kind", ["path", "limit", "target", "tampered", "command"])
def test_invalid_metadata_or_selection_or_command_fails_closed(kind):
    delivery = Delivery()
    def resolve(plan):
        if kind in {"target", "tampered"}:
            return ("a.py",)
        return ("../outside",) if kind == "path" else ("a.py",) * 5001

    select = selection
    request = call("get_git_diff")
    if kind in {"target", "tampered"}:
        request = call("read_file", path="a.py")
        def select(target):
            return (selection("wrong.py") if kind == "target" else
                    selection(target).model_copy(update={"sha256": "0" * 64}))
    elif kind == "command":
        request = call("run_tests", command="pytest -p untrusted")
    guard = RuleAccessGate(ToolAccessPlanner(), SimpleNamespace(select=select), delivery,
                           resolver=SimpleNamespace(resolve=resolve))
    with pytest.raises(RuleError):
        guard.check(request)
    assert not delivery.pending


def test_non_source_exemptions_are_explicit_and_cannot_include_source_tools():
    guard = gate(Delivery(), non_source_tools=frozenset({"get_task_state"}))
    assert guard.check(call("get_task_state")).allowed
    with pytest.raises(RuleError, match="description_missing"):
        guard.check(call("get_unknown_state"))
    with pytest.raises(RuleError, match="cannot_bypass"):
        gate(Delivery(), non_source_tools=frozenset({"apply_patch"}))


class ScriptedModel(BaseLLM):
    def __init__(self, batches, before=None):
        super().__init__(LLMConfig(model_name="zero-provider"))
        self.batches, self.before, self.index = iter(batches), before, 0

    def complete(self, messages, tools=()):
        self.index += 1
        if self.before is not None:
            self.before(self.index)
        calls = next(self.batches)
        return LLMResponse(model_name="zero-provider", message=Message(
            role=MessageRole.ASSISTANT, tool_calls=calls, content=None if calls else "done"))


def test_actual_agent_denies_same_batch_read_and_patch_before_preimage(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("pending rules allowed a source read or patch normalization")

    reader, patcher = ReadFileTool(tmp_path), ApplyPatchTool(tmp_path)
    monkeypatch.setattr(reader, "execute", forbidden)
    monkeypatch.setattr(patcher, "_normalize_patch", forbidden)
    model = ScriptedModel([(
        ToolCall(id="read", name="read_file", arguments={"path": "a.py"}),
        ToolCall(id="patch", name="apply_patch", arguments={
            "patch": "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n*** End Patch"}),
    ), ()])
    agent = MinimalAgent(model, ToolRegistry((reader, patcher)), AgentConfig())
    delivery = Delivery()
    agent.rule_access_gate = gate(delivery)
    state = agent.run("inspect and repair")
    results = [message for message in agent.history.snapshot() if message.role is MessageRole.TOOL]
    assert len(results) == 2 and all("rules_pending" in item.content for item in results)
    assert state.file_read_calls == 0 and state.test_runs == 0
    assert not agent.history.pending_tool_call_ids
    assert len(delivery.pending) == 2


def test_actual_agent_gate_precedes_test_prepare_and_test_count(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    tool = RunTestsTool(workspace, evidence_dir=tmp_path / "evidence")
    monkeypatch.setattr(tool, "prepare", lambda call: pytest.fail("prepare preceded rule delivery"))
    agent = MinimalAgent(ScriptedModel([(
        ToolCall(id="test", name="run_tests", arguments={"command": "pytest tests/a.py"}),
    ), ()]), ToolRegistry((tool,)), AgentConfig())
    agent.rule_access_gate = gate(Delivery(), resolver=SimpleNamespace(
        resolve=lambda plan: ("tests/a.py",)))
    state = agent.run("test")
    assert state.test_runs == 0 and state.rejected_test_calls == 0
    assert any("rules_pending" in (item.content or "") for item in agent.history.snapshot())


def test_actual_agent_rechecks_rules_before_returning_a_cached_source_body(tmp_path, monkeypatch):
    path = tmp_path / "a.py"
    path.write_text("current body", encoding="utf-8")
    tool = ReadFileTool(tmp_path)
    execute = tool.execute
    reads = []

    def read(request):
        reads.append(request.id)
        return execute(request)

    monkeypatch.setattr(tool, "execute", read)
    delivery = Delivery()
    delivery.delivered = True

    def before(index):
        if index == 2:
            delivery.delivered = False

    model = ScriptedModel([
        (ToolCall(id="first", name="read_file", arguments={"path": "a.py"}),),
        (ToolCall(id="cached", name="read_file", arguments={"path": "a.py"}),), (),
    ], before=before)
    agent = MinimalAgent(model, ToolRegistry((tool,)), AgentConfig())
    agent.rule_access_gate = gate(delivery)
    state = agent.run("inspect")
    results = {item.tool_call_id: item for item in agent.history.snapshot()
               if item.role is MessageRole.TOOL}
    assert reads == ["first"] and state.file_read_calls == 1
    assert "current body" in results["first"].content
    assert "rules_pending" in results["cached"].content
    assert "current body" not in results["cached"].content


def test_capability_policy_denies_before_rule_selection(tmp_path):
    model = ScriptedModel([(
        ToolCall(id="patch", name="apply_patch", arguments={"patch": "invalid"}),), ()])
    tool = ApplyPatchTool(tmp_path)
    agent = MinimalAgent(model, ToolRegistry((tool,)), AgentConfig())
    agent.tool_dispatcher = ToolDispatcher(CapabilityPolicy(
        CapabilityCatalog.for_tool_names(("apply_patch",)), ExecutionProfile.INSPECT))
    agent.rule_access_gate = RuleAccessGate(ToolAccessPlanner(), SimpleNamespace(
        select=lambda path: pytest.fail("policy was bypassed")), Delivery())
    agent.run("inspect only")
    assert any("profile_denied" in (item.content or "") for item in agent.history.snapshot())
