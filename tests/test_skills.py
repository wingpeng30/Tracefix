from collections import deque
from pathlib import Path
from tomllib import loads

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
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from tracefix.context import ContextConfig
from tracefix.exceptions import ToolExecutionError
from tracefix.tools import create_default_tool_registry
from tracefix.tools.skills import SkillActivationTool


class FixtureLLM(BaseLLM):
    def __init__(self, calls):
        super().__init__(LLMConfig(model_name="fixture"))
        self.calls = deque(calls)
        self.requests = []

    def complete(self, messages, tools=()):
        self.requests.append((messages, tools))
        call = self.calls.popleft()
        return LLMResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                content="done" if call is None else None,
                tool_calls=() if call is None else (call,),
            ),
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2, cost_usd=0),
            model_name="fixture",
            finish_reason="stop" if call is None else "tool_calls",
        )


class NoopTool:
    @property
    def spec(self):
        return ToolSpec(name="noop", description="Fixture tool")

    def execute(self, call):
        return ToolResult(call_id=call.id, tool_name=call.name, success=True)


def test_catalog_only_when_enabled_and_skill_load_survives_compaction():
    skill = SkillActivationTool(Path(__file__).parent / "fixtures" / "tracefix-skills")
    load = ToolCall(id="load", name="load_skill", arguments={"name": "demo"})
    llm = FixtureLLM(
        [
            load,
            ToolCall(id="noop-1", name="noop"),
            ToolCall(id="noop-2", name="noop"),
            ToolCall(id="noop-3", name="noop"),
            None,
        ]
    )
    agent = MinimalAgent(
        llm,
        ToolRegistry([skill, NoopTool()]),
        AgentConfig(
            skills_enabled=True,
            context=ContextConfig(
                compaction_trigger_tokens=1,
                context_window_tokens=10_000,
                retain_ratio=0.1,
            ),
        ),
    )

    agent.run("debug")

    first_messages, first_tools = llm.requests[0]
    assert "demo" in first_messages[1].content
    assert "Fixture instruction." not in "\n".join(m.content or "" for m in first_messages)
    assert "load_skill" in {spec.name for spec in first_tools}
    assert any(
        message.metadata.get("kind") == "skill_instructions"
        and "Fixture instruction." in (message.content or "")
        for message in agent.history.snapshot()
    )
    assert agent.state.context_metrics.compaction_count > 0
    assert any(
        "Fixture instruction." in (message.content or "")
        for message in llm.requests[-1][0]
    )


def test_skill_repeated_activation_is_deduplicated():
    skill = SkillActivationTool()
    call = ToolCall(id="one", name="load_skill", arguments={"name": "tracefix-debugging"})
    loaded = skill.execute(call)
    repeated = skill.execute(call.model_copy(update={"id": "two"}))
    assert loaded.output["kind"] == "skill"
    assert repeated.output == {"name": "tracefix-debugging", "already_loaded": True}


def test_reference_must_be_loaded_after_skill_and_stay_inside_root():
    skill = SkillActivationTool(Path(__file__).parent / "fixtures" / "tracefix-skills")
    reference_call = ToolCall(
        id="ref", name="load_skill", arguments={"name": "demo", "reference": "references/guide.md"}
    )
    with pytest.raises(ToolExecutionError, match="activate the skill"):
        skill.execute(reference_call)
    skill.execute(ToolCall(id="activate", name="load_skill", arguments={"name": "demo"}))
    result = skill.execute(reference_call)
    assert result.output["content"].strip() == "Fixture reference."
    traversal = reference_call.model_copy(
        update={
            "id": "escape",
            "arguments": {"name": "demo", "reference": "references/../SKILL.md"},
        }
    )
    with pytest.raises(ToolExecutionError, match="outside"):
        skill.execute(traversal)


def test_frontmatter_rejects_duplicate_fields_and_ignores_allowed_tools():
    with pytest.raises(ToolExecutionError, match="duplicate"):
        SkillActivationTool._parse_frontmatter(
            'name: demo\nname: demo\ndescription: desc', "demo"
        )
    parsed = SkillActivationTool._parse_frontmatter(
        'name: demo\ndescription: desc\nallowed-tools: shell', "demo"
    )
    assert parsed["allowed-tools"] == "shell"
    assert SkillActivationTool().spec.name == "load_skill"


def test_default_agent_has_no_skill_catalog_or_tool_description():
    agent = MinimalAgent(FixtureLLM([None]))
    agent.run("ordinary")
    messages, tools = agent.llm.requests[0]
    assert not any("available TraceFix skills" in (message.content or "") for message in messages)
    assert "load_skill" not in {spec.name for spec in tools}


def test_runtime_registry_keeps_skills_opt_in():
    workspace = Path(__file__).resolve().parents[1]
    assert "load_skill" not in create_default_tool_registry(workspace).names
    assert "load_skill" in create_default_tool_registry(workspace, skills_enabled=True).names


def test_package_metadata_keeps_extras_and_bundles_skills():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    config = loads(pyproject.read_text(encoding="utf-8"))
    assert "dev" in config["project"]["optional-dependencies"]
    assert "llm" in config["project"]["optional-dependencies"]
    assert "skills/*/SKILL.md" in config["tool"]["setuptools"]["package-data"]["tracefix"]
