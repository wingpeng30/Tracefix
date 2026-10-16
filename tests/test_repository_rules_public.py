import json
import subprocess
from pathlib import Path

import pytest

from tracefix import (
    AgentConfig,
    BaseLLM,
    LLMResponse,
    Message,
    MessageRole,
    RunConfig,
    TokenUsage,
    ToolCall,
    TraceFixRunner,
)
from tracefix.checkpoint import CheckpointStore
from tracefix.features import FeatureResolutionError, resolve_features
from tracefix.runtime import config_identity_sha256


class PagingModel(BaseLLM):
    def __init__(self, config):
        super().__init__(config)
        self.requests = []

    def complete(self, messages, tools=()):
        self.requests.append(tuple(messages))
        last = next((m for m in reversed(messages) if m.role == MessageRole.TOOL), None)
        if last is None:
            arguments = {"query": "needle", "path": "a.py", "max_results": 1}
        else:
            output = json.loads(last.content)["output"]
            continuation = output.get("continuation")
            arguments = continuation["arguments"] if continuation else None
        calls = ([ToolCall(id=f"page-{len(messages)}", name="search_code", arguments=arguments)]
                 if arguments is not None else [])
        return LLMResponse(model_name=self.config.model_name, usage=TokenUsage(cost_usd=0),
            message=Message(role=MessageRole.ASSISTANT, tool_calls=calls,
                            content=None if calls else "Located both lines"))


def make_repo(root):
    root.mkdir()
    (root / "AGENTS.md").write_text("保持两行针点🙂", encoding="utf-8")
    (root / "a.py").write_text("# needle first\n# needle middle\n", encoding="utf-8")
    for args in [("init",), ("add", "."), ("-c", "user.name=Fixture", "-c",
                 "user.email=fixture@example.invalid", "commit", "-m", "base")]:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_public_local_run_inspection_and_recovery_keep_rule_and_page_state(tmp_path, monkeypatch):
    source = tmp_path / "source"
    make_repo(source)
    models = []

    def factory(config):
        model = PagingModel(config)
        models.append(model)
        return model

    save = CheckpointStore.save

    def interrupt(self, payload, *, sequence, pending_calls=()):
        result = save(self, payload, sequence=sequence, pending_calls=pending_calls)
        if payload["agent"]["state"]["step_count"] == 1:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(CheckpointStore, "save", interrupt)
    first = TraceFixRunner(llm_factory=factory).run(RunConfig(
        repo=source, task="Find both needle lines", model_name="offline/replay",
        output_dir=tmp_path / "runs", env_file=None, execution_backend="local",
        execution_profile="inspect", repository_rules_enabled=True,
        harness_foundations_enabled=True, agent_config=AgentConfig(max_steps=8)))
    assert first.status.value == "interrupted"
    root = Path(first.result_path).parent
    assert not (root / "repo-map.json").exists()
    inspection = TraceFixRunner.inspect(root)
    assert inspection["resumable"], inspection["reasons"]
    monkeypatch.setattr(CheckpointStore, "save", save)
    result = TraceFixRunner(llm_factory=factory).resume(root)
    assert result.status.value == "completed", result.error
    assert result.test_runs == 0 and result.search_calls == 3
    assert len(models) == 2 and len(models[0].requests) == 1
    assert any(m.metadata.get("kind") == "repository_rules" for m in models[1].requests[0])
    saved = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))["payload"]
    state = saved["agent"]["repository_rules"]["payload"]
    assert len(state["search"]["payload"]["cursors"]) == 2
    assert saved["agent"]["task_state"]["state"]["requirements"]


def test_rules_false_keeps_legacy_config_hash_but_true_does_not():
    config = RunConfig(repo=Path("."), task="fixture", execution_backend="local")
    legacy = config.model_dump(mode="json", exclude={"repository_rules_enabled"})
    import hashlib

    expected = hashlib.sha256(config.model_dump_json(
        exclude={"repository_rules_enabled"}).encode()).hexdigest()
    assert config_identity_sha256(config, legacy) == expected
    config.repository_rules_enabled = True
    assert config_identity_sha256(config, legacy) != expected


def test_rules_docker_requires_ordinary_recovery_before_any_model():
    with pytest.raises(FeatureResolutionError) as caught:
        resolve_features(RunConfig(repo=Path("."), task="fixture", repository_rules_enabled=True))
    assert caught.value.context["reason_code"] == "rules_ordinary_recovery_required"
    resolve_features(RunConfig(repo=Path("."), task="fixture", repository_rules_enabled=True,
                               docker_recovery_enabled=True))


def test_docker_assembly_preserves_executor_directory_without_reading_source(tmp_path):
    from types import SimpleNamespace

    from tracefix.repository_rules.domain import RuleSelection
    from tracefix.runtime import _assemble_repository_rules
    from tracefix.tools import create_default_tool_registry

    source = tmp_path / "source"
    make_repo(source)
    original = create_default_tool_registry(source, skills_enabled=False)
    ports = SimpleNamespace(select=lambda path: RuleSelection.create(path, (), ()),
                            resolve=lambda plan: (), read_bytes=lambda path: None)
    backend = SimpleNamespace(repository_rule_ports=lambda: ports,
                              session=SimpleNamespace(tools=original.specs()))
    config = RunConfig(repo=source, task="fixture", repository_rules_enabled=True,
                       docker_recovery_enabled=True)
    registry, gate = _assemble_repository_rules(config, original, run_id="run-a", workspace=None,
                                                backend=backend)
    assert registry.executor_specs == original.specs()
    assert registry.get("search_code").spec != original.get("search_code").spec
    assert gate.selector is ports and gate.resolver is ports
