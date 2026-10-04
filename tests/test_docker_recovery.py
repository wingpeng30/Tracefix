"""Batch identity and bridge journal checks reject ambiguous recovery artifacts."""

import hashlib
import json

import pytest

from examples.replay_ordinary import _git
from tracefix.checkpoint import CheckpointError
from tracefix.docker_recovery import owned_file, tool_identity, validate_batch
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


@pytest.mark.parametrize("field,value,reason", [
    ("schema_version", 2, "sequence changed"),
    ("batch_id", "invalid", "batch identity"),
    ("archive", "../outside", "escapes"),
    ("image_id", "other-image", "image identity"),
    ("bridge_state_sha256", "corrupt", "bridge state"),
    ("journal_size", -1, "journal prefix"),
    ("journal_sha256", "corrupt", "journal prefix"),
    ("test_evidence", {"../outside": "corrupt"}, "evidence path"),
])
def test_corrupt_batch_bindings_fail_before_recovery(batch, field, value, reason):
    root, snapshot = batch
    original_batch = snapshot["batch_id"]
    snapshot[field] = value
    (root / "docker-checkpoints" / f"{original_batch}.json").write_text(
        json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(CheckpointError, match=reason):
        validate_batch(root, snapshot, sequence=1, image_id="pinned-image")


def test_sidecar_and_evidence_corruption_are_rejected(batch):
    root, snapshot = batch
    sidecar = root / "docker-checkpoints" / f'{snapshot["batch_id"]}.json'
    sidecar.write_text("{}", encoding="utf-8")
    with pytest.raises(CheckpointError, match="metadata changed"):
        validate_batch(root, snapshot, sequence=1, image_id="pinned-image")
    directory = root / "test-evidence"
    directory.mkdir()
    (directory / "result.json").write_bytes(b"changed")
    snapshot["test_evidence"] = {"result.json": hashlib.sha256(b"original").hexdigest()}
    sidecar.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(CheckpointError, match="test evidence changed"):
        validate_batch(root, snapshot, sequence=1, image_id="pinned-image")


@pytest.mark.parametrize("found,owner,removes", [
    (False, "owned", False), (True, "owned", True), (True, "other", False),
])
def test_previous_container_removal_requires_full_identity(
    tmp_path, monkeypatch, found, owner, removes,
):
    from types import SimpleNamespace

    from tracefix.docker_backend import DockerToolBackend
    from tracefix.exceptions import WorkspaceError

    backend = DockerToolBackend(task_id="tracefix-ordinary", input_root=tmp_path,
                                run_dir=tmp_path, run_id="owned", profile="ordinary",
                                image_id="sha256:" + "a" * 64)
    previous = "b" * 64
    backend.container_id = "c" * 64
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        value = (previous + "\n" if found else "") if command[1] == "ps" else owner + "\n"
        return SimpleNamespace(stdout=value.encode())

    monkeypatch.setattr("tracefix.docker_backend._run", run)
    if found and owner != "owned":
        with pytest.raises(WorkspaceError, match="ownership changed"):
            backend.remove_previous_container({"container_id": previous})
    else:
        backend.remove_previous_container({"container_id": previous})
    assert any(command[1:3] == ["rm", "-f"] for command in commands) is removes


@pytest.fixture
def inspected(batch, monkeypatch):
    from types import SimpleNamespace

    from tracefix.agent import AgentState, MinimalAgent
    from tracefix.checkpoint import CheckpointStore
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
    registry = create_default_tool_registry(config.repo)
    identity["tool_sha256"] = tool_identity(registry.specs())
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
    return root, manifest, config, calls


def test_inspect_docker_session_without_original_container(inspected):
    from tracefix.docker_recovery import inspect_session

    root, manifest, config, calls = inspected
    outcome = inspect_session(root, manifest, config, implementation_sha256="code",
                              config_sha256="config")
    assert outcome["resumable"] is True, outcome
    assert len(calls) == 1 and calls[0][1:3] == ["image", "inspect"]
    (root / "result.json").write_text('{"status":"completed"}', encoding="utf-8")
    assert "run is not interrupted" in inspect_session(
        root, manifest, config, implementation_sha256="code", config_sha256="config")["reasons"]


@pytest.mark.parametrize("fault,reason", [
    ("usage", "unresolved model accounting"), ("memory", "unresolved memory request"),
    ("runtime", "runtime memory is incomplete"), ("test", "passing test does not match"),
    ("trace", "trajectory prefix changed"), ("suffix", "uncommitted model or tool outcome"),
])
def test_inspect_rejects_unresolved_or_corrupt_agent_evidence(inspected, fault, reason):
    from tracefix.checkpoint import CheckpointStore
    from tracefix.docker_recovery import inspect_session

    root, manifest, config, _ = inspected
    store = CheckpointStore(root, manifest["identity"])
    payload = store.load().payload
    if fault == "usage":
        payload["agent"]["state"]["usage_complete"] = False
    elif fault == "memory":
        payload["agent"]["state"]["memory_status"] = {"status": "outcome_unknown"}
    elif fault == "runtime":
        payload["agent"]["memory"] = {}
    elif fault == "test":
        payload["agent"]["memory"]["_last_test_evidence"] = {
            "valid": True, "source_sha256": "different-products",
        }
    elif fault == "trace":
        payload["trace_sha256"] = "changed"
    else:
        (root / "trajectory.jsonl").write_bytes(b'{"event_type":"model_requested"}\n')
    store.path.unlink()
    store.save(payload, sequence=1)
    if fault == "trace":
        with pytest.raises(CheckpointError, match=reason):
            inspect_session(root, manifest, config, implementation_sha256="code",
                            config_sha256="config")
    else:
        outcome = inspect_session(root, manifest, config, implementation_sha256="code",
                                  config_sha256="config")
        assert any(reason in item for item in outcome["reasons"])


@pytest.mark.parametrize("budget", ["step_count", "test_runs", "input_tokens", "output_tokens",
                                   "active_seconds"])
def test_continue_refuses_each_exhausted_cumulative_budget(inspected, budget):
    from tracefix.checkpoint import CheckpointStore
    from tracefix.docker_recovery import inspect_session

    root, manifest, config, _ = inspected
    config.conversation_enabled = True
    manifest["schema_version"] = 2
    store = CheckpointStore(root, manifest["identity"])
    payload = store.load().payload
    limits = {"step_count": config.agent_config.max_steps,
              "test_runs": config.agent_config.max_test_runs,
              "input_tokens": config.agent_config.max_input_tokens,
              "output_tokens": config.agent_config.max_output_tokens,
              "active_seconds": config.agent_config.wall_time_seconds}
    payload["agent"]["active_seconds"] = 0
    if budget == "active_seconds":
        payload["agent"][budget] = limits[budget]
    else:
        payload["agent"]["state"][budget] = limits[budget]
    store.path.unlink()
    store.save(payload, sequence=1)
    (root / "result.json").write_text('{"status":"completed","turn_number":1}', encoding="utf-8")
    turn = root / "turns/0001"
    attempt = "a" * 32
    directory = turn / attempt
    directory.mkdir(parents=True)
    names = {"patch.diff", "trajectory.jsonl", "session.json", "validation.json", "result.json"}
    for name in names:
        (directory / name).write_bytes(b"immutable round artifact")
    record = {"schema_version": 1, "turn_number": 1, "attempt": attempt,
              "artifacts": dict.fromkeys(
                  names, hashlib.sha256(b"immutable round artifact").hexdigest())}
    (turn / "latest.json").write_text(json.dumps(record), encoding="utf-8")
    outcome = inspect_session(root, manifest, config, implementation_sha256="code",
                              config_sha256="config", for_continue=True)
    assert "cumulative session budget exhausted" in outcome["reasons"]


@pytest.mark.parametrize("fault", ["attempt", "missing-artifact", "changed-artifact", "round"])
def test_continue_rejects_corrupt_round_archive(inspected, fault):
    from tracefix.checkpoint import CheckpointStore
    from tracefix.docker_recovery import inspect_session

    root, manifest, config, _ = inspected
    config.conversation_enabled = True
    manifest["schema_version"] = 2
    store = CheckpointStore(root, manifest["identity"])
    payload = store.load().payload
    payload["agent"]["active_seconds"] = 0
    store.path.unlink()
    store.save(payload, sequence=1)
    (root / "result.json").write_text('{"status":"completed","turn_number":1}', encoding="utf-8")
    turn = root / "turns/0001"
    attempt = "a" * 32
    directory = turn / attempt
    directory.mkdir(parents=True)
    names = {"patch.diff", "trajectory.jsonl", "session.json", "validation.json", "result.json"}
    for name in names:
        (directory / name).write_bytes(b"committed")
    record = {"schema_version": 1, "turn_number": 1, "attempt": attempt,
              "artifacts": dict.fromkeys(names, hashlib.sha256(b"committed").hexdigest())}
    if fault == "attempt":
        record["attempt"] = "../outside"
    elif fault == "missing-artifact":
        record["artifacts"].pop("validation.json")
    elif fault == "changed-artifact":
        (directory / "patch.diff").write_bytes(b"changed")
    else:
        record["turn_number"] = 2
    (turn / "latest.json").write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(CheckpointError, match="conversation round"):
        inspect_session(root, manifest, config, implementation_sha256="code",
                        config_sha256="config", for_continue=True)


@pytest.mark.parametrize("fault", ["timeout", "missing", "other-image"])
def test_inspect_does_not_accept_unverified_image(inspected, monkeypatch, fault):
    from types import SimpleNamespace

    from tracefix.docker_recovery import inspect_session

    root, manifest, config, _ = inspected

    def inspect_image(*args, **kwargs):
        if fault == "timeout":
            raise TimeoutError("unavailable engine")
        return SimpleNamespace(returncode=1 if fault == "missing" else 0, stdout="other-image")

    monkeypatch.setattr("tracefix.docker_recovery.subprocess.run", inspect_image)
    if fault == "timeout":
        with pytest.raises(CheckpointError, match="inspection is unavailable"):
            inspect_session(root, manifest, config, implementation_sha256="code",
                            config_sha256="config")
    else:
        outcome = inspect_session(root, manifest, config, implementation_sha256="code",
                                  config_sha256="config")
        assert "saved Docker image is unavailable or changed" in outcome["reasons"]
