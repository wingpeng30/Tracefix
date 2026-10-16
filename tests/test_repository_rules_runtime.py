import json
import subprocess
from pathlib import Path

import pytest

from tracefix import (
    AgentConfig,
    BaseLLM,
    LLMConfig,
    LLMResponse,
    Message,
    MessageRole,
    MinimalAgent,
)
from tracefix.messages import ToolCall
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.runtime import assemble_local_rules
from tracefix.tools import create_default_tool_registry
from tracefix.tools.base import ToolRegistry


class Model(BaseLLM):
    def __init__(self):
        super().__init__(LLMConfig(model_name="zero-provider"))
        self.requests = []

    def complete(self, messages, tools=()):
        self.requests.append(tuple(messages))
        if len(self.requests) == 1:
            arguments = {"query": "needle", "glob": "**/*.py"}
        elif len(self.requests) == 2:
            last = next(m for m in reversed(messages) if m.role == MessageRole.TOOL)
            arguments = json.loads(last.content)["output"]["continuation"]["arguments"]
        else:
            return LLMResponse(model_name="zero-provider", message=Message(
                role=MessageRole.ASSISTANT, content="done"))
        return LLMResponse(model_name="zero-provider", message=Message(
            role=MessageRole.ASSISTANT, tool_calls=[ToolCall(
                id=f"search-{len(self.requests)}", name="search_code", arguments=arguments)]))


def tools_at(root):
    if not (root / ".git").exists():
        for arguments in [("init",), ("add", "."), ("-c", "user.name=Fixture", "-c",
                          "user.email=fixture@example.invalid", "commit", "--allow-empty",
                          "-m", "fixture")]:
            subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                             check=True, capture_output=True, text=True, encoding="utf-8")
    assert root.resolve() == Path(result.stdout.strip()).resolve()
    return create_default_tool_registry(root, skills_enabled=False)


def test_local_runtime_replaces_only_search_and_delivers_before_read(tmp_path):
    (tmp_path / "AGENTS.md").write_text("保留规则🙂", encoding="utf-8")
    (tmp_path / "a.py").write_text("needle middle", encoding="utf-8")
    tools = tools_at(tmp_path)
    runtime = assemble_local_rules(tools, root=tmp_path, run_id="run-a")
    assert runtime.tools.names == tools.names
    for tool in tools:
        if tool.spec.name != "search_code":
            assert runtime.tools.get(tool.spec.name) is tool
    assert runtime.tools.get("search_code").spec != tools.get("search_code").spec
    model = Model()
    agent = MinimalAgent(model, runtime.tools, AgentConfig(max_steps=4))
    agent.rule_access_gate = runtime.gate
    agent.run("search")
    outputs = [json.loads(m.content)["output"] for m in agent.history
               if m.role == MessageRole.TOOL]
    assert outputs[0]["rules_pending"] and outputs[0]["matches"] == []
    assert outputs[1]["matches"][0]["path"] == "a.py"
    assert any(m.metadata.get("kind") == "repository_rules" for m in model.requests[1])
    assert agent.checkpoint_payload()["repository_rules"]["payload"]["run_id"] == "run-a"


def test_protected_rule_and_source_paths_fail_before_read(tmp_path):
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "a.py").write_text("needle", encoding="utf-8")
    runtime = assemble_local_rules(tools_at(tmp_path), root=tmp_path, run_id="run-a",
                                   protected_dirs=("private",))
    call = ToolCall(id="read", name="read_file", arguments={"path": "private/a.py"})
    with pytest.raises(RuleError, match="rules_target_is_internal"):
        runtime.gate.check(call)


def test_mcp_guard_bound_explicitly_and_missing_guard_cannot_bypass(tmp_path):
    class Mcp:
        def bind_source_result_guard(self, guard):
            self.guard = guard

    manager = Mcp()
    runtime = assemble_local_rules(tools_at(tmp_path), root=tmp_path, run_id="run-a", mcp=manager)
    assert manager.guard is runtime.gate.source_validators["mcp_serena_symbols"]
    absent = assemble_local_rules(tools_at(tmp_path), root=tmp_path, run_id="run-a")
    with pytest.raises(RuleError, match="rules_tool_access_description_missing"):
        absent.gate.check(ToolCall(id="mcp", name="mcp_serena_symbols",
                                   arguments={"relative_path": "a.py"}))


def test_invalid_pytest_config_rejected_during_assembly(tmp_path):
    with pytest.raises(RuleError):
        assemble_local_rules(tools_at(tmp_path), root=tmp_path, run_id="run-a",
                             pytest_config="../pytest.ini")


def test_remote_executor_identity_uses_negotiated_schema_not_host_search_adapter(tmp_path):
    from tracefix.docker_recovery import tool_identity
    from tracefix.exceptions import RunConfigurationError
    from tracefix.runtime import RunConfig, TraceFixRunner

    original = tools_at(tmp_path)
    local = assemble_local_rules(original, root=tmp_path, run_id="run-a")
    hosted = ToolRegistry(tuple(local.tools), executor_specs=original.specs())
    config = RunConfig(repo=tmp_path, task="fixture", docker_recovery_enabled=True,
                       repository_rules_enabled=True)
    dispatcher = TraceFixRunner._capability_dispatcher(config, hosted)
    assert TraceFixRunner._executor_tool_identity(config, hosted, dispatcher) == tool_identity(
        original.specs())
    assert tool_identity(hosted.specs()) != tool_identity(original.specs())
    TraceFixRunner._capability_identity(config, hosted, dispatcher)
    with pytest.raises(RunConfigurationError, match="fixed executor catalog"):
        TraceFixRunner._executor_tool_identity(config, local.tools, dispatcher)
