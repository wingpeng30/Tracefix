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
from tracefix.tools.skills import SkillActivationTool, SkillLimits


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


def test_skill_catalog_is_explicit_path_free_and_content_is_not_repeated_in_tool_message():
    skill = SkillActivationTool(Path(__file__).parent / "fixtures" / "tracefix-skills")
    entry = skill.skill_catalog[0]
    assert (entry.name, entry.version, len(entry.sha256)) == ("demo", "0.1.0", 64)
    assert not hasattr(entry, "path")

    llm = FixtureLLM([ToolCall(id="load", name="load_skill", arguments={"name": "demo"}), None])
    agent = MinimalAgent(llm, ToolRegistry([skill]), AgentConfig(skills_enabled=True))
    agent.run("debug")
    messages = llm.requests[-1][0]
    skill_result = next(message for message in messages if message.role is MessageRole.TOOL)
    assert "Fixture instruction." not in (skill_result.content or "")
    assert any(
        message.metadata.get("kind") == "skill_instructions"
        and "Fixture instruction." in (message.content or "")
        for message in messages
    )


def test_skill_reference_deduplication_and_byte_budgets():
    root = Path(__file__).parent / "fixtures" / "tracefix-skills"
    skill = SkillActivationTool(root, limits=SkillLimits(max_total_bytes=160))
    main = ToolCall(id="activate", name="load_skill", arguments={"name": "demo"})
    reference = ToolCall(
        id="reference", name="load_skill",
        arguments={"name": "demo", "reference": "references/guide.md"},
    )
    skill.execute(main)
    loaded = skill.execute(reference)
    repeated = skill.execute(reference.model_copy(update={"id": "reference-again"}))
    assert loaded.output["content"] == "Fixture reference.\n"
    assert repeated.output["already_loaded"] is True

    limited = SkillActivationTool(root, limits=SkillLimits(max_total_bytes=1))
    with pytest.raises(ToolExecutionError, match="budget exceeded"):
        limited.execute(main)


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


def _write_skill(root: Path, name: str, body: str, *, reference: str | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    skill_path = directory / "SKILL.md"
    skill_path.write_text(
        (
            f"---\nname: {name}\ndescription: A fixture skill.\nmetadata:\n"
            f"  version: 1.0\n---\n\n{body}\n"
        ),
        encoding="utf-8",
    )
    if reference is not None:
        references = directory / "references"
        references.mkdir()
        (references / "guide.md").write_text(reference, encoding="utf-8")
    return skill_path


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    [
        ("name: demo\ndescription: desc\nunknown: x", "unsupported skill frontmatter"),
        ("name: demo\ndescription: \"bad", "invalid quoted skill metadata"),
        ("name: demo\ndescription: value: nested", "quote skill metadata"),
        ("name: demo\ndescription: desc\nmetadata: inline", "metadata must be a YAML mapping"),
        (
            "name: demo\ndescription: desc\n  version: 1\n  version: 2",
            "unsupported skill metadata syntax",
        ),
    ],
)
def test_skill_frontmatter_rejects_unsupported_and_malformed_yaml(
    frontmatter: str, message: str
) -> None:
    with pytest.raises(ToolExecutionError, match=message):
        SkillActivationTool._parse_frontmatter(frontmatter, "demo")


def test_skill_catalog_fails_closed_on_identity_and_metadata_problems(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "demo", "instructions")
    duplicate = root / "other"
    duplicate.mkdir()
    (duplicate / "SKILL.md").write_text(
        "---\nname: demo\ndescription: duplicate\n---\n\nbody\n", encoding="utf-8"
    )
    with pytest.raises(ToolExecutionError, match="invalid or duplicated"):
        SkillActivationTool(root)

    (duplicate / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
    with pytest.raises(ToolExecutionError, match="malformed frontmatter"):
        SkillActivationTool(root)

    (duplicate / "SKILL.md").write_text(
        "---\nname: other\ndescription: missing terminator", encoding="utf-8"
    )
    with pytest.raises(ToolExecutionError, match="unterminated frontmatter"):
        SkillActivationTool(root)


def test_skill_activation_rejects_drift_and_active_limit(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    first = _write_skill(root, "first", "instruction one")
    _write_skill(root, "second", "instruction two")
    skill = SkillActivationTool(root, limits=SkillLimits(max_active_skills=1))
    def activate(name: str, call_id: str) -> ToolCall:
        return ToolCall(id=call_id, name="load_skill", arguments={"name": name})

    first.write_text(first.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")
    with pytest.raises(ToolExecutionError, match="changed after catalog"):
        skill.execute(activate("first", "drift"))
    first.write_text(
        (
            "---\nname: first\ndescription: A fixture skill.\nmetadata:\n"
            "  version: 1.0\n---\n\ninstruction one\n"
        ),
        encoding="utf-8",
    )
    skill.execute(activate("first", "activate"))
    with pytest.raises(ToolExecutionError, match="maximum number"):
        skill.execute(activate("second", "second"))


def test_reference_read_errors_are_explicit_and_references_are_read_only(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "demo", "instructions", reference="reference text")
    skill = SkillActivationTool(root)
    activate = ToolCall(id="activate", name="load_skill", arguments={"name": "demo"})
    skill.execute(activate)
    def reference(value: str, call_id: str) -> ToolCall:
        return ToolCall(
            id=call_id, name="load_skill", arguments={"name": "demo", "reference": value}
        )
    with pytest.raises(ToolExecutionError, match="does not exist"):
        skill.execute(reference("references/missing.md", "missing"))
    with pytest.raises(ToolExecutionError, match="relative path"):
        skill.execute(reference("C:/outside.md", "absolute"))
    with pytest.raises(ToolExecutionError, match="outside the approved"):
        skill.execute(reference("SKILL.md", "main"))
    limited = SkillActivationTool(root, limits=SkillLimits(max_reference_bytes=2))
    limited.execute(activate)
    with pytest.raises(ToolExecutionError, match="reference text exceeds"):
        limited.execute(reference("references/guide.md", "too-large"))


def test_skill_catalog_skips_non_skills_and_rejects_symlink_and_oversize(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "ordinary-directory").mkdir()
    (root / "README.md").write_text("catalog root note", encoding="utf-8")
    assert SkillActivationTool(root).skill_catalog == ()

    linked = root / "linked"
    linked.mkdir()
    _write_skill(root, "regular", "instructions")
    real_is_symlink = Path.is_symlink

    def mark_directory_as_link(path: Path) -> bool:
        return path == linked or real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", mark_directory_as_link)
    with pytest.raises(ToolExecutionError, match="directory cannot be a symbolic link"):
        SkillActivationTool(root)
    monkeypatch.setattr(Path, "is_symlink", real_is_symlink)

    (linked / "SKILL.md").write_text("x" * (16 * 1024 + 8193), encoding="utf-8")
    with pytest.raises(ToolExecutionError, match="discovery size limit"):
        SkillActivationTool(root)


def test_skill_reference_directory_and_read_failures_are_explicit(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    skill_path = _write_skill(root, "demo", "instructions")
    skill = SkillActivationTool(root)
    activate = ToolCall(id="activate", name="load_skill", arguments={"name": "demo"})
    skill.execute(activate)
    reference = ToolCall(
        id="reference",
        name="load_skill",
        arguments={"name": "demo", "reference": "references/guide.md"},
    )
    with pytest.raises(ToolExecutionError, match="reference does not exist"):
        skill.execute(reference)

    def unreadable(path: Path, *args, **kwargs):
        if path == skill_path:
            raise UnicodeError("invalid fixture encoding")
        return original_read_text(path, *args, **kwargs)

    original_read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(ToolExecutionError, match="could not be read"):
        skill.execute(activate.model_copy(update={"id": "read-failure"}))


def test_skill_reference_budget_and_duplicate_identity_are_checked(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "demo", "x", reference="g" * 1000)
    skill = SkillActivationTool(root, limits=SkillLimits(max_total_bytes=128))
    activation = ToolCall(id="activate", name="load_skill", arguments={"name": "demo"})
    skill.execute(activation)
    reference = ToolCall(
        id="reference",
        name="load_skill",
        arguments={"name": "demo", "reference": "references/guide.md"},
    )
    with pytest.raises(ToolExecutionError, match="budget exceeded.*128 bytes used"):
        skill.execute(reference)

    generous = SkillActivationTool(root, limits=SkillLimits(max_total_bytes=2048))
    generous.execute(activation)
    loaded = generous.execute(reference)
    assert loaded.output["content"] == "g" * 1000
    generous._loaded[("demo", "references/guide.md")] = ("1.0", "different-hash")
    with pytest.raises(ToolExecutionError, match="changed after it was loaded"):
        generous.execute(reference.model_copy(update={"id": "changed"}))


def test_skill_recovery_restores_reference_budget_and_identity(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "demo", "instructions", reference="reference text")
    original = SkillActivationTool(root)
    activation = ToolCall(id="main", name="load_skill", arguments={"name": "demo"})
    reference = ToolCall(
        id="reference", name="load_skill",
        arguments={"name": "demo", "reference": "references/guide.md"},
    )
    original.execute(activation)
    original.execute(reference)
    state = original.recovery_state()

    restored = SkillActivationTool(root)
    restored.restore_recovery_state(state)
    assert restored.recovery_state() == state
    assert restored.execute(reference.model_copy(update={"id": "again"})).output["already_loaded"]
    assert restored.recovery_state()["loaded_bytes"] == state["loaded_bytes"]

    (root / "demo" / "references" / "guide.md").write_text("changed", encoding="utf-8")
    with pytest.raises(ToolExecutionError, match="content changed"):
        SkillActivationTool(root).restore_recovery_state(state)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"catalog": []}, "catalog changed"),
        ({"loaded": "bad"}, "state is invalid"),
        ({"activated": "bad"}, "state is invalid"),
        ({"loaded": ["bad"]}, "entry is invalid"),
        ({"loaded": [{"name": "unknown", "path": "SKILL.md"}]}, "identity is invalid"),
        ({"loaded": [{"name": "demo", "path": "../../outside"}]}, "path changed"),
        ({"loaded_bytes": 9999}, "byte usage is invalid"),
        ({"activated": []}, "activation set is invalid"),
    ],
)
def test_skill_recovery_rejects_corrupt_state(
    tmp_path: Path, mutation: dict, message: str
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "demo", "instructions")
    original = SkillActivationTool(root)
    original.execute(ToolCall(id="main", name="load_skill", arguments={"name": "demo"}))
    state = {**original.recovery_state(), **mutation}
    with pytest.raises(ToolExecutionError, match=message):
        SkillActivationTool(root).restore_recovery_state(state)
