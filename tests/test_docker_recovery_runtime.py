"""Recovery failures preserve historical results and retain durable diagnostics."""

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tracefix import runtime
from tracefix.agent import AgentConfig, AgentStatus
from tracefix.checkpoint import CheckpointStore
from tracefix.exceptions import WorkspaceError
from tracefix.provenance import RunProvenance
from tracefix.runtime import RunConfig, RunResult, TraceFixRunner


@pytest.mark.parametrize("memory", [False, True])
def test_restored_runner_retains_actual_tool_history_tests_and_cumulative_usage(
    tmp_path, monkeypatch, memory,
):
    """Host adapter supplements the separately required real Linux Docker matrix."""
    from examples.replay_ordinary import _git
    from tracefix.docker_recovery import tool_identity
    from tracefix.memory import atomic_json, digest
    from tracefix.memory_replay import RecordedMemoryClient
    from tracefix.messages import ToolCall
    from tracefix.models import LiteLLMAdapter
    from tracefix.tools import create_default_tool_registry
    from tracefix.workspace_snapshot import export_workspace, restore_workspace

    source = tmp_path / "source"
    source.mkdir()
    (source / "tests").mkdir()
    (source / "widget.py").write_text("def next_page(page):\n    return page\n", encoding="utf-8")
    (source / "tests/test_widget.py").write_text(
        "from widget import next_page\ndef test_page(): assert next_page(1) == 2\n",
        encoding="utf-8")
    _git(source, "init")
    _git(source, "add", ".")
    _git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-m", "base")
    closed = []

    class HostBackend:
        def __init__(self, **kwargs):
            self.root = kwargs["run_dir"]
            self.image_id = kwargs["image_id"]
            self.container_id = uuid4().hex * 2
            self.repo_map = None
            self.protected = {".tracefix-build-tmp"}

        def prepare(self, commit, source_repo, package_root, **kwargs):
            self.workspace = self.root / ("host-adapter-" + uuid4().hex)
            TraceFixRunner._clone_repository(source_repo, self.workspace)
            (self.root / "agent-base.tar").write_bytes(b"fixed test baseline")
            if kwargs.get("recovery_snapshot"):
                saved = kwargs["recovery_snapshot"]
                restore_workspace(self.workspace, self.root / saved["archive"], saved["metadata"])
                self.protected.update(saved["bridge_state"]["protected_dirs"])
                if ".tracefix-test-tmp" in self.protected:
                    (self.workspace / ".tracefix-test-tmp").mkdir()
            self.tools = create_default_tool_registry(
                self.workspace, evidence_dir=self.root / "test-evidence",
                protected_dirs=self.protected, skills_enabled=kwargs["skills_enabled"],
                skills_root=kwargs.get("skills_root"), skill_limits=kwargs["skill_limits"],
                test_python_executable=sys.executable,
            )
            if kwargs.get("recovery_snapshot") and kwargs["skills_enabled"]:
                self.tools.get("load_skill").restore_recovery_state(saved["bridge_state"]["skills"])
            self.session = SimpleNamespace(call=lambda name, args: self.tools.get(name).execute(
                ToolCall(id=uuid4().hex, name=name, arguments=args)))
            self.workspace_preparation = {"container_id": self.container_id, "backend": "docker"}
            return self.tools

        def save_snapshot(self, sequence):
            directory = self.root / "docker-checkpoints"
            directory.mkdir(exist_ok=True)
            batch = f"{sequence:08d}-" + uuid4().hex
            archive = directory / f"{batch}.tar"
            with archive.open("wb") as stream:
                metadata = export_workspace(self.workspace, stream)
            bridge = {"schema_version": 1, "protected_dirs": sorted(self.protected),
                      "diff": self.session.call("get_git_diff", {"context_lines": 3}).output,
                      "skills": self.tools.get("load_skill").recovery_state()
                      if "load_skill" in self.tools.names else None}
            journal = self.root / "tool-events.jsonl"
            journal.write_bytes(b"")
            snapshot = {"schema_version": 1, "sequence": sequence, "batch_id": batch,
                        "archive": str(archive.relative_to(self.root)), "metadata": metadata,
                        "bridge_state": bridge, "bridge_state_sha256": digest(bridge),
                        "journal_size": 0, "journal_sha256": hashlib.sha256(b"").hexdigest(),
                        "baseline_sha256": hashlib.sha256(b"fixed test baseline").hexdigest(),
                        "image_id": self.image_id, "container_id": self.container_id,
                        "tool_sha256": tool_identity(self.tools.specs()), "test_evidence": {}}
            atomic_json(directory / f"{batch}.json", snapshot)
            return snapshot

        def export_evidence(self):
            pass

        def set_phase(self, phase):
            pass

        def remove_previous_container(self, snapshot):
            assert snapshot["container_id"] != self.container_id

        def close(self, *, remove):
            closed.append(self.container_id)

    monkeypatch.setattr(runtime, "DockerToolBackend", HostBackend)
    original_run = subprocess.run

    def run(command, **kwargs):
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=image + "\n")
        return original_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    original_save = CheckpointStore.save

    def pause(store, payload, **kwargs):
        saved = original_save(store, payload, **kwargs)
        if kwargs["sequence"] == 3:
            raise KeyboardInterrupt()
        return saved

    monkeypatch.setattr(CheckpointStore, "save", pause)
    image = "sha256:" + "a" * 64
    client = RecordedMemoryClient(recall=False)
    runner = TraceFixRunner(lambda config: LiteLLMAdapter(config, client=client))
    config = RunConfig(repo=source, task="Fix pagination", output_dir=tmp_path / "runs",
                       env_file=None, model_name="offline/recovery", execution_backend="docker",
                       docker_profile="ordinary", docker_image_id=image,
                       docker_recovery_enabled=True, memory_enabled=memory,
                       test_target="tests/test_widget.py", source_import="widget")
    interrupted = runner.run(config)
    assert interrupted.status == "interrupted" and interrupted.step_count == 2
    root = Path(interrupted.result_path).parent
    assert runner.inspect(root)["resumable"]
    monkeypatch.setattr(CheckpointStore, "save", original_save)
    completed = runner.resume(root)
    assert completed.status == "completed" and completed.agent_validation_status == "verified"
    assert completed.step_count == (7 if memory else 6)
    assert completed.input_tokens == 100 * completed.step_count
    assert len(set(closed)) == 2
    assert completed.workspace == f"docker://{closed[-1]}/work/agent"
    assert completed.workspace != interrupted.workspace
    if memory:
        assert completed.memory_status["status"] == "verified"
    assert "return page + 1" in (root / "patch.diff").read_text(encoding="utf-8")


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_prepare_and_cleanup_failures_are_durable_before_any_model(
    tmp_path, monkeypatch, cleanup_fails,
):
    root = tmp_path / "run"
    root.mkdir()
    now = datetime.now(UTC)
    prior = RunResult(
        run_id="run", source_repo=str(tmp_path), model_name="offline/recovery",
        status=AgentStatus.INTERRUPTED, cost_complete=True, usd_cny_rate=7.2,
        started_at=now, finished_at=now, duration_seconds=1,
        trace_path=str(root / "trajectory.jsonl"), diff_path=str(root / "patch.diff"),
        result_path=str(root / "result.json"), agent_config=AgentConfig(),
        provenance=RunProvenance(tracefix_version="test", task_sha256="a" * 64,
                                 model_parameters={}, python_version="3.12", platform="test",
                                 dependency_versions={}),
    )
    (root / "result.json").write_text(prior.model_dump_json(), encoding="utf-8")
    (root / "patch.diff").write_bytes(b"historical patch")
    identity = {"source_commit": "saved"}
    CheckpointStore(root, identity).save({"docker_snapshot": {}}, sequence=1)
    preserved = {name: (root / name).read_bytes() for name in (
        "result.json", "patch.diff", "checkpoint.json",
    )}
    closed = []

    class Backend:
        container_id = "owned-new-container"

        def __init__(self, **kwargs):
            pass

        def prepare(self, *args, **kwargs):
            raise WorkspaceError("injected restore failure")

        def close(self, *, remove):
            closed.append(remove)
            if cleanup_fails:
                raise WorkspaceError("injected cleanup failure")

    monkeypatch.setattr(runtime, "DockerToolBackend", Backend)
    constructed = []
    runner = TraceFixRunner(lambda config: constructed.append(config))
    config = RunConfig(repo=tmp_path, task="fix", execution_backend="docker",
                       docker_profile="ordinary", docker_image_id="sha256:" + "a" * 64,
                       docker_recovery_enabled=True, source_import="widget",
                       test_target="tests/test_widget.py", model_name="offline/recovery")
    expected_error = "cleanup failure" if cleanup_fails else "restore failure"
    with pytest.raises(WorkspaceError, match=expected_error):
        runner._resume_docker_locked(root, {"identity": identity}, config)
    assert constructed == [] and closed == [True]
    assert {name: (root / name).read_bytes() for name in preserved} == preserved
    diagnostics = [json.loads(path.read_text(encoding="utf-8"))
                   for path in root.glob("recovery-failure-*.json")]
    assert {row["stage"] for row in diagnostics} == (
        {"resume", "cleanup"} if cleanup_fails else {"resume"})
    assert all(row["checkpoint_sequence"] == 1 for row in diagnostics)
