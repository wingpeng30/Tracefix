from __future__ import annotations

import hashlib
import io
import json
import sys
from types import SimpleNamespace

import pytest

import tracefix.agent_bridge as agent_bridge
from tracefix.capabilities import CapabilityCatalog, CapabilityPolicy, ExecutionProfile
from tracefix.messages import ToolCall
from tracefix.tools import ToolResult
from tracefix.tools.base import ToolSpec


class _Tool:
    def __init__(self, name: str):
        self.name = name
        self.spec = ToolSpec(name=name, description="bridge fixture")
        self.prepared: list[ToolCall] = []
        self.on_process_started = None

    def prepare(self, call: ToolCall) -> None:
        self.prepared.append(call)

    def execute(self, call: ToolCall) -> ToolResult:
        if self.on_process_started is not None:
            self.on_process_started(call.id)
        return ToolResult(
            call_id=call.id,
            tool_name=call.name,
            success=True,
            output={"accepted": True},
        )


class _Registry:
    def __init__(self):
        self.names = ("search_code", "run_tests")
        self.tool = _Tool("search_code")
        self.test_tool = _Tool("run_tests")
        self.skill_catalog = (
            SimpleNamespace(
                model_dump=lambda **_kwargs: {
                    "name": "tracefix-debugging",
                    "description": "debug",
                    "version": "1.0.0",
                    "sha256": "a" * 64,
                }
            ),
        )

    def specs(self):
        return ()

    def get(self, name: str):
        if name == "search_code":
            return self.tool
        if name == "run_tests":
            return self.test_tool
        raise KeyError(name)


def _call(call_id: str, name: str, arguments: dict) -> str:
    return json.dumps({"id": call_id, "name": name, "arguments": arguments})


def test_bridge_handshake_rpc_prepare_errors_and_process_started(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    evidence = tmp_path / "evidence"
    workspace.mkdir()
    evidence.mkdir()
    environment_file = tmp_path / "environment.json"
    environment_file.write_text('{"SAFE_VALUE":"1"}', encoding="utf-8")
    registry = _Registry()
    created = {}

    def create_registry(root, **kwargs):
        created["root"] = root
        created.update(kwargs)
        return registry

    class Indexer:
        def __init__(self, root, config):
            created["repo_map_root"] = root
            created["repo_map_config"] = config

        def build(self):
            return "index"

        def make_repo_map(self, _index, task):
            return SimpleNamespace(model_dump=lambda **_kwargs: {"task": task})

    prepare = {"id": "nested", "name": "search_code", "arguments": {"q": "x"}}
    lines = [
        _call("test", "run_tests", {}),
        _call("prepare", "__prepare__", prepare),
        _call("unknown", "missing", {}),
        "not-json",
    ]
    monkeypatch.setattr(agent_bridge, "create_default_tool_registry", create_registry)
    monkeypatch.setattr("tracefix.repository.RepositoryIndexer", Indexer)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent_bridge",
            "--workspace", str(workspace),
            "--evidence", str(evidence),
            "--python", sys.executable,
            "--environment-json", str(environment_file),
            "--pythonpath", str(workspace),
            "--timeout", "9",
            "--run-id", "run-1",
            "--repo-map-task", "find symbols",
            "--repo-map-config", '{"max_candidate_files":2}',
            "--skills-enabled",
            "--skill-limits-json", '{"max_active_skills":2}',
        ],
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n"))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)

    assert agent_bridge.main() == 0

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert events[0]["type"] == "hello"
    assert events[0]["run_id"] == "run-1"
    assert events[0]["skill_catalog"][0]["name"] == "tracefix-debugging"
    assert events[0]["repo_map"] == {"task": "find symbols"}
    assert created["root"] == workspace
    assert created["test_environment_variables"] == {"SAFE_VALUE": "1"}
    assert created["test_timeout_seconds"] == 9
    assert created["skill_limits"].max_active_skills == 2
    assert registry.tool.prepared[0].name == "search_code"
    assert events[1] == {"type": "process_started", "call_id": "test"}
    assert events[2]["result"]["success"] is True
    assert events[3]["result"]["success"] is True
    assert events[4]["result"]["success"] is False
    assert events[5]["type"] == "result"
    assert events[5]["call_id"] == "invalid-call"
    assert events[5]["result"]["success"] is False


def test_artifact_enabled_bridge_exposes_negotiated_protocol(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    evidence = tmp_path / "evidence"
    artifact_root = tmp_path / "artifact-evidence" / "tool-artifacts"
    workspace.mkdir()
    evidence.mkdir()
    environment_file = tmp_path / "environment.json"
    environment_file.write_text('{"TOKEN":"a-private-token-value"}', encoding="utf-8")
    registry = _Registry()
    created = {}

    def create_registry(root, **kwargs):
        created.update(kwargs)
        return registry

    monkeypatch.setattr(agent_bridge, "create_default_tool_registry", create_registry)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent_bridge", "--workspace", str(workspace), "--evidence", str(evidence),
            "--python", sys.executable, "--environment-json", str(environment_file),
            "--run-id", "run-artifact", "--artifacts-enabled", "--artifact-root",
            str(artifact_root),
        ],
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)

    assert agent_bridge.main() == 0
    hello = json.loads(output.getvalue().splitlines()[0])
    assert hello["artifact_protocol"] == {
        "version": 1,
        "max_artifact_bytes": 8 * 1024 * 1024,
        "max_run_bytes": 64 * 1024 * 1024,
        "max_run_artifacts": 128,
    }
    assert created["artifact_store"].run_id == "run-artifact"
    manifest = json.loads((artifact_root / "manifest.json").read_text(encoding="utf-8"))
    assert "a-private-token-value" not in json.dumps(manifest)


def test_bridge_binds_read_only_executor_policy_file_before_handshake(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    evidence = tmp_path / "evidence"
    workspace.mkdir()
    evidence.mkdir()
    environment_file = tmp_path / "environment.json"
    environment_file.write_text("{}", encoding="utf-8")
    registry = _Registry()
    registry.skill_catalog = ()
    catalog = CapabilityCatalog.for_tool_names(registry.names)
    policy = CapabilityPolicy(catalog, ExecutionProfile.WORKSPACE_WRITE)
    policy_file = tmp_path / "capability-policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "policy_version": policy.VERSION,
                "execution_profile": policy.profile.value,
                "capability_catalog_sha256": catalog.sha256,
                "capability_policy_sha256": policy.sha256,
                "capabilities": [
                    item.model_dump(mode="json") for item in catalog.descriptors
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        agent_bridge, "create_default_tool_registry", lambda *_args, **_kwargs: registry
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent_bridge",
            "--workspace", str(workspace),
            "--evidence", str(evidence),
            "--python", sys.executable,
            "--environment-json", str(environment_file),
            "--run-id", "run-policy",
            "--capability-policy-file", str(policy_file),
        ],
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)

    assert agent_bridge.main() == 0
    hello = json.loads(output.getvalue().splitlines()[0])
    assert hello["capability_policy_sha256"] == policy.sha256
    assert hello["executor_policy_config_sha256"] == hashlib.sha256(
        policy_file.read_bytes()
    ).hexdigest()


def test_bridge_rejects_policy_bundle_profile_drift_before_handshake(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    evidence = tmp_path / "evidence"
    workspace.mkdir()
    evidence.mkdir()
    environment_file = tmp_path / "environment.json"
    environment_file.write_text("{}", encoding="utf-8")
    registry = _Registry()
    registry.skill_catalog = ()
    wrong_policy = CapabilityPolicy(
        CapabilityCatalog.for_tool_names(registry.names), ExecutionProfile.INSPECT
    )
    policy_file = tmp_path / "capability-policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "policy_version": wrong_policy.VERSION,
                "execution_profile": wrong_policy.profile.value,
                "capability_catalog_sha256": wrong_policy.catalog.sha256,
                "capability_policy_sha256": wrong_policy.sha256,
                "capabilities": [
                    item.model_dump(mode="json")
                    for item in wrong_policy.catalog.descriptors
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        agent_bridge, "create_default_tool_registry", lambda *_args, **_kwargs: registry
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent_bridge",
            "--workspace", str(workspace),
            "--evidence", str(evidence),
            "--python", sys.executable,
            "--environment-json", str(environment_file),
            "--run-id", "run-policy-drift",
            "--capability-policy-file", str(policy_file),
        ],
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)

    with pytest.raises(ValueError, match="does not match tool catalog"):
        agent_bridge.main()
    assert output.getvalue() == ""


@pytest.mark.parametrize("environment", ['[]', '{"BAD":1}'])
def test_bridge_rejects_non_string_environment_values(tmp_path, monkeypatch, environment):
    workspace = tmp_path / "workspace"
    evidence = tmp_path / "evidence"
    workspace.mkdir()
    evidence.mkdir()
    environment_file = tmp_path / "environment.json"
    environment_file.write_text(environment, encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent_bridge", "--workspace", str(workspace), "--evidence", str(evidence),
            "--python", sys.executable, "--environment-json", str(environment_file),
            "--run-id", "run-2",
        ],
    )
    with pytest.raises(ValueError, match="string mapping"):
        agent_bridge.main()


@pytest.mark.parametrize("names", [None, [], ["../escape"], [".tracefix-test-tmp"], [1]])
def test_restore_rejects_invalid_protected_identity_before_mutation(tmp_path, names):
    protected = {"original"}
    with pytest.raises(ValueError, match="invalid bridge"):
        agent_bridge.restore_state(SimpleNamespace(names=()), protected, tmp_path,
                                   {"schema_version": 1, "protected_dirs": names})
    assert protected == {"original"}
    assert list(tmp_path.iterdir()) == []


def test_restore_recreates_temporary_identity_and_skill_accounting(tmp_path):
    restored = []
    skill = SimpleNamespace(restore_recovery_state=restored.append)
    tools = SimpleNamespace(names=("load_skill",), get=lambda _name: skill)
    protected = {"old"}
    state = {"schema_version": 1,
             "protected_dirs": [".tracefix-build-tmp", ".tracefix-test-tmp"],
             "skills": {"loaded": ["immutable-version"]}}
    agent_bridge.restore_state(tools, protected, tmp_path, state)
    assert restored == [state["skills"]]
    assert protected == set(state["protected_dirs"])
    assert (tmp_path / ".tracefix-test-tmp").is_dir()
    with pytest.raises(ValueError, match="already exists"):
        agent_bridge.restore_state(tools, protected, tmp_path, state)


def test_restore_refuses_skills_when_disabled(tmp_path):
    with pytest.raises(ValueError, match="disabled Skills"):
        agent_bridge.restore_state(SimpleNamespace(names=()), set(), tmp_path,
                                   {"schema_version": 1,
                                    "protected_dirs": [".tracefix-build-tmp"],
                                    "skills": {"loaded": []}})


@pytest.mark.parametrize("success,truncated", [(False, False), (True, True)])
def test_checkpoint_refuses_incomplete_diff(success, truncated):
    diff = SimpleNamespace(execute=lambda _call: ToolResult(
        call_id="checkpoint-diff", tool_name="get_git_diff", success=success,
        output={"truncated": truncated}, error=None if success else "diff failed"))
    tools = SimpleNamespace(names=(), get=lambda _name: diff)
    with pytest.raises(ValueError, match="complete product diff"):
        agent_bridge.recovery_state(tools, {".tracefix-build-tmp"})
