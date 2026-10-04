"""Windows transport fault tests supplement the required real Linux Docker runs."""

import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from examples.replay_ordinary import _git
from tracefix.docker_backend import DockerToolBackend
from tracefix.docker_recovery import validate_batch
from tracefix.exceptions import WorkspaceError
from tracefix.tools import ToolResult
from tracefix.workspace_snapshot import export_workspace, restore_stream


@pytest.fixture
def transport(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "product.py").write_bytes(b"product\n")
    _git(source, "init")
    _git(source, "add", ".")
    _git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-m", "base")
    root = tmp_path / "run"
    root.mkdir()
    (root / "test-evidence").mkdir()
    (root / "test-evidence/test.json").write_bytes(b"verified evidence")
    (root / "agent-base.tar").write_bytes(b"baseline")
    (root / "tool-events.jsonl").write_bytes(b"")
    backend = DockerToolBackend(task_id="tracefix-ordinary", input_root=root, run_dir=root,
                                run_id="run", profile="ordinary", image_id="sha256:" + "a" * 64)
    backend.container_id = "b" * 64
    backend.image_id = "sha256:" + "a" * 64
    backend.python = "python"
    backend.workspace_preparation = {"container_interpreter": {"version": "3.11"}}
    backend.session = SimpleNamespace(tools=(), call=lambda name, args: ToolResult(
        call_id="internal", tool_name=name, success=True,
        output={"schema_version": 1, "protected_dirs": [".tracefix-build-tmp"],
                "skills": None, "diff": {"diff": ""}}))
    monkeypatch.setattr(backend, "export_evidence", lambda: None)
    target = tmp_path / "restored"

    original_run = subprocess.run

    def run(command, **kwargs):
        if command[0] == "git":
            return original_run(command, **kwargs)
        if "export" in command:
            metadata = export_workspace(source, kwargs["stdout"])
            return SimpleNamespace(returncode=0, stderr=json.dumps(metadata).encode())
        if "restore-stream" in command:
            metadata = json.loads((root / "restore-metadata.json").read_text(encoding="utf-8"))
            restore_stream(target, kwargs["stdin"], metadata)
            return SimpleNamespace(returncode=0, stderr=b"")
        raise AssertionError(command)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(backend, "_copy_file_into", lambda *args: None)
    monkeypatch.setattr("tracefix.docker_backend._run", lambda *args, **kwargs: None)
    return backend, source, target


def test_snapshot_publication_and_streamed_transport_bind_real_products(transport):
    backend, source, target = transport
    snapshot = backend.save_snapshot(1)
    validate_batch(backend.run_dir, snapshot, sequence=1, image_id=backend.image_id)
    backend.restore_snapshot(snapshot)
    assert (target / "product.py").read_bytes() == (source / "product.py").read_bytes()
    expected_evidence = hashlib.sha256(b"verified evidence").hexdigest()
    assert snapshot["test_evidence"]["test.json"] == expected_evidence
    assert not list((backend.run_dir / "docker-checkpoints").glob("*.partial"))


@pytest.mark.parametrize("failure", ["export", "metadata", "publish", "evidence"])
def test_failed_publication_preserves_prior_archive_and_cleans_partial(
    transport, monkeypatch, failure,
):
    backend, _, _ = transport
    prior = backend.save_snapshot(1)
    original = (backend.run_dir / prior["archive"]).read_bytes()
    if failure in {"export", "metadata"}:
        monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
            returncode=1 if failure == "export" else 0, stderr=b"injected export failure"))
    elif failure == "publish":
        monkeypatch.setattr("tracefix.docker_backend.os.replace", lambda *args: (
            _ for _ in ()).throw(OSError("injected publication failure")))
    else:
        monkeypatch.setattr(backend, "export_evidence", lambda: (
            _ for _ in ()).throw(WorkspaceError("injected evidence failure")))
    with pytest.raises((WorkspaceError, ValueError, OSError)):
        backend.save_snapshot(2)
    assert (backend.run_dir / prior["archive"]).read_bytes() == original
    assert not list((backend.run_dir / "docker-checkpoints").glob("*.partial"))


def test_container_restore_failure_is_reported(transport, monkeypatch):
    backend, _, _ = transport
    saved = backend.save_snapshot(1)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stderr=b"injected restore failure"))
    with pytest.raises(WorkspaceError, match="could not restore"):
        backend.restore_snapshot(saved)
