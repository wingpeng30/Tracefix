"""Batch identity and bridge journal checks reject ambiguous recovery artifacts."""

import hashlib
import json

import pytest

from examples.replay_ordinary import _git
from tracefix.checkpoint import CheckpointError
from tracefix.docker_recovery import owned_file, validate_batch
from tracefix.memory import digest
from tracefix.workspace_snapshot import export_workspace


def test_tool_identity_preserves_contract_with_equivalent_json_numbers():
    from tracefix.docker_recovery import tool_identity
    from tracefix.tools.base import ToolSpec

    def spec(bound):
        return ToolSpec(name="test", description="contract", input_schema={
            "type": "object", "properties": {"timeout": {"minimum": bound}}})

    assert tool_identity([spec(1)]) == tool_identity([spec(1.0)])
    assert tool_identity([spec(1)]) != tool_identity([spec(1.1)])
    assert tool_identity([spec(1)]) != tool_identity([spec(True)])


def test_installed_recovery_entry_requires_docker_and_dispatches(tmp_path, monkeypatch, capsys):
    from tracefix import docker_recovery_replay
    from tracefix.reproduction import main

    with pytest.raises(SystemExit) as error:
        main(["--scenario", "docker-recovery", "--output", str(tmp_path)])
    assert error.value.code == 2
    calls = []

    def replay(output, image_id):
        calls.append((output, image_id))
        return {"accepted": True, "provider_calls": 0}

    monkeypatch.setattr(docker_recovery_replay, "run_recovery_replay", replay)
    assert main(["--scenario", "docker-recovery", "--backend", "docker", "--image-id",
                 "pinned", "--output", str(tmp_path)]) == 0
    assert calls == [(tmp_path, "pinned")]
    assert '"accepted": true' in capsys.readouterr().out


@pytest.fixture
def batch(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "widget.py").write_bytes(b"product\n")
    _git(source, "init")
    _git(source, "add", ".")
    _git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-m", "base")
    directory = tmp_path / "docker-checkpoints"
    directory.mkdir()
    identity = "00000001-" + "a" * 32
    archive = directory / f"{identity}.tar"
    with archive.open("wb") as stream:
        metadata = export_workspace(source, stream)
    (tmp_path / "agent-base.tar").write_bytes(b"saved baseline")
    journal = b'{"event":"handshake"}\n'
    (tmp_path / "tool-events.jsonl").write_bytes(journal)
    snapshot = {"schema_version": 1, "sequence": 1, "batch_id": identity,
                "archive": f"docker-checkpoints/{identity}.tar", "metadata": metadata,
                "image_id": "pinned-image", "baseline_sha256": hashlib.sha256(
                    b"saved baseline").hexdigest(), "bridge_state": {},
                "bridge_state_sha256": digest({}), "test_evidence": {},
                "journal_size": len(journal), "journal_sha256": hashlib.sha256(journal).hexdigest()}
    (directory / f"{identity}.json").write_text(json.dumps(snapshot), encoding="utf-8")
    return tmp_path, snapshot


@pytest.mark.parametrize("tool", ["get_git_diff", "__recovery_state__"])
def test_valid_batch_and_known_final_read(batch, tool):
    root, snapshot = batch
    validate_batch(root, snapshot, sequence=1, image_id="pinned-image")
    with (root / "tool-events.jsonl").open("a", encoding="utf-8") as stream:
        for event in ("dispatch_started", "request_sent", "result_received"):
            stream.write(json.dumps({"event": event, "call_id": "final",
                                     "tool": tool}) + "\n")
    validate_batch(root, snapshot, sequence=1, image_id="pinned-image")


@pytest.mark.parametrize("tool,complete", [("apply_patch", False), ("get_git_diff", False),
                                         ("apply_patch", True), ("__recovery_state__", False)])
def test_unknown_or_uncommitted_tool_refuses_recovery(batch, tool, complete):
    root, snapshot = batch
    with (root / "tool-events.jsonl").open("a", encoding="utf-8") as stream:
        for event in (["dispatch_started", "result_received"] if complete
                      else ["dispatch_started"]):
            stream.write(json.dumps({"event": event, "call_id": "unknown", "tool": tool}) + "\n")
    with pytest.raises(CheckpointError, match="unknown|uncommitted"):
        validate_batch(root, snapshot, sequence=1, image_id="pinned-image")


@pytest.mark.parametrize("name", ["../outside", "C:/outside", "/outside", "missing"])
def test_owned_artifact_rejects_unsafe_or_missing_paths(tmp_path, name):
    with pytest.raises(CheckpointError):
        owned_file(tmp_path, name)


def test_baseline_corruption_preserves_saved_checkpoint(batch):
    root, snapshot = batch
    (root / "agent-base.tar").write_bytes(b"corrupt")
    with pytest.raises(CheckpointError, match="baseline changed"):
        validate_batch(root, snapshot, sequence=1, image_id="pinned-image")


def test_inspect_docker_session_without_original_container(batch, monkeypatch):
    from types import SimpleNamespace

    from tracefix.agent import AgentState, MinimalAgent
    from tracefix.checkpoint import CheckpointStore
    from tracefix.docker_recovery import inspect_session
    from tracefix.runtime import RunConfig
    from tracefix.tools import create_default_tool_registry

    root, snapshot = batch
    snapshot["bridge_state"] = {"skills": None, "protected_dirs": [".tracefix-build-tmp"],
                                "diff": {"diff": ""}}
    snapshot["bridge_state_sha256"] = digest(snapshot["bridge_state"])
    (root / "docker-checkpoints" / f'{snapshot["batch_id"]}.json').write_text(
        json.dumps(snapshot), encoding="utf-8")
    config = RunConfig(repo=root / "source", task="fix", execution_backend="docker",
                       docker_profile="ordinary", docker_image_id="pinned-image",
                       test_target="tests/test_widget.py", source_import="widget",
                       docker_recovery_enabled=True)
    identity = {"implementation_sha256": "code", "config_sha256": "config",
                "repo_map_sha256": hashlib.sha256(b"").hexdigest()}
    registry = create_default_tool_registry(root)
    identity["tool_sha256"] = hashlib.sha256(json.dumps(
        [spec.model_dump(mode="json") for spec in registry.specs()],
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    snapshot["tool_sha256"] = identity["tool_sha256"]
    (root / "docker-checkpoints" / f'{snapshot["batch_id"]}.json').write_text(
        json.dumps(snapshot), encoding="utf-8")
    manifest = {"identity": identity, "checkpoint_supported": True, "schema_version": 1}
    trace = b'{"event_type":"task_finished","payload":{"state":{"status":"interrupted"}}}\n'
    (root / "trajectory.jsonl").write_bytes(trace)
    state = AgentState(task="fix")
    CheckpointStore(root, identity).save({"docker_snapshot": snapshot,
        "agent": {"state": state.model_dump(mode="json"), "history": [],
                  "memory": dict.fromkeys(MinimalAgent._RECOVERY_FIELDS)}, "skills": None,
        "protected_dirs": [".tracefix-build-tmp"], "trace_size": 0,
        "trace_sha256": hashlib.sha256(b"").hexdigest(),
        "workspace_diff_sha256": hashlib.sha256(b"").hexdigest()}, sequence=1)
    calls = []

    def inspect_image(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="pinned-image\n")

    monkeypatch.setattr("tracefix.docker_recovery.subprocess.run", inspect_image)
    outcome = inspect_session(root, manifest, config, implementation_sha256="code",
                              config_sha256="config")
    assert outcome["resumable"] is True
    assert len(calls) == 1 and calls[0][1:3] == ["image", "inspect"]
    (root / "result.json").write_text('{"status":"completed"}', encoding="utf-8")
    assert "run is not interrupted" in inspect_session(
        root, manifest, config, implementation_sha256="code", config_sha256="config")["reasons"]
