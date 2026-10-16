"""Recovery failures preserve historical results and retain durable diagnostics."""

import hashlib
import json
import os
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
@pytest.mark.parametrize("jobs", [False, True])
def test_restored_runner_retains_actual_tool_history_tests_and_cumulative_usage(
    tmp_path, monkeypatch, memory, jobs,
):
    """Host adapter supplements the separately required real Linux Docker matrix."""
    if jobs and os.name != "nt":
        pytest.skip("Windows host adapter uses Job Object; real Linux ordinary CI covers Docker")
    from examples.replay_ordinary import _git
    from tracefix.capabilities import CallerOrigin
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
            self.timeout_seconds = kwargs["timeout_seconds"]
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
            artifacts = None
            if jobs:
                from tracefix.artifacts import ArtifactStore

                self.job_settings = kwargs["job_settings"]
                artifacts = ArtifactStore(self.root / ("adapter-artifacts-" + uuid4().hex),
                    workspace=self.workspace, run_id=self.job_settings.logical_run_id,
                    streaming_enabled=True)
                if kwargs.get("recovery_snapshot"):
                    with (self.root / saved["artifact_archive"]).open("rb") as stream:
                        artifacts.import_archive(stream)
            self.artifacts = artifacts
            self.tools = create_default_tool_registry(
                self.workspace, evidence_dir=self.root / "test-evidence",
                protected_dirs=self.protected, skills_enabled=kwargs["skills_enabled"],
                skills_root=kwargs.get("skills_root"), skill_limits=kwargs["skill_limits"],
                test_python_executable=sys.executable,
                artifact_store=artifacts,
            )
            from tracefix.capabilities import (
                CapabilityCatalog,
                CapabilityPolicy,
                executor_policy_document,
                host_control_policy,
            )
            from tracefix.skill_snapshot import EMPTY_SKILL_BUNDLE_SHA256

            self.executor_specs = self.tools.specs()
            executor_names = list(self.tools.names)
            if jobs:
                from tracefix.jobs.constants import JOB_CONTROL_CAPABILITIES

                executor_names.extend(JOB_CONTROL_CAPABILITIES)
            executor_catalog = CapabilityCatalog.for_tool_names(executor_names)
            executor_policy = CapabilityPolicy(executor_catalog, kwargs["execution_profile"])
            control_policy = host_control_policy(kwargs["execution_profile"])
            policy_document = executor_policy_document(
                kwargs["execution_profile"],
                artifacts_enabled=kwargs["artifacts_enabled"],
                skills_enabled=kwargs["skills_enabled"],
                **({"jobs_enabled": True} if jobs else {}),
            )
            policy_bytes = json.dumps(
                policy_document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.host_control_policy = control_policy
            self.skills_bundle_sha256 = EMPTY_SKILL_BUNDLE_SHA256
            self.executor_policy_config_sha256 = hashlib.sha256(policy_bytes).hexdigest()
            self.capability_catalog_sha256 = executor_catalog.sha256
            self.capability_policy_sha256 = executor_policy.sha256
            if kwargs.get("recovery_snapshot") and kwargs["skills_enabled"]:
                self.tools.get("load_skill").restore_recovery_state(saved["bridge_state"]["skills"])
            self.session = SimpleNamespace(call=lambda name, args: self.tools.get(name).execute(
                ToolCall(id=uuid4().hex, name=name, arguments=args)),
                capability_catalog_sha256=self.capability_catalog_sha256,
                capability_policy_sha256=self.capability_policy_sha256)
            if jobs:
                from tracefix.agent_bridge import _execute_job_control
                from tracefix.jobs.process_tree import WindowsJobScope
                from tracefix.jobs.protocol import WorkerTicketMirror
                from tracefix.jobs.service import JobService
                from tracefix.jobs.worker import JobWorkerController

                settings, executor = self.job_settings, self.tools.get("run_tests")
                tickets = WorkerTicketMirror(run_id=settings.logical_run_id,
                    worker_epoch=settings.worker_epoch, host_owner_epoch=settings.host_owner_epoch)
                service = JobService(self.root / ("adapter-worker-" + settings.worker_epoch),
                    run_id=settings.logical_run_id, checkout_id="f" * 64, ledger=tickets,
                    artifact_store=artifacts, test_tool=executor, authorize=lambda call: None,
                    scope_factory=WindowsJobScope, evaluate=executor.evaluate_job_result,
                    verify_source=executor.verify_prepared_source,
                    session_deadline=settings.session_deadline)
                self.worker = JobWorkerController(service=service, tickets=tickets,
                                                    authorize=lambda capability: None)
                original = self.session.call

                def call(name, args):
                    if name.startswith("__job_"):
                        return _execute_job_control(self.worker,
                            ToolCall(id=uuid4().hex, name=name, arguments=args))
                    return original(name, args)

                self.session.call = call
                self.job_protocol = self.session.job_protocol = {
                    "version": 1, "worker_epoch": settings.worker_epoch,
                    "host_owner_epoch": settings.host_owner_epoch,
                    "logical_run_id": settings.logical_run_id, "context_sha256": "f" * 64}
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
                        "capability_catalog_sha256": self.capability_catalog_sha256,
                        "capability_policy_sha256": self.capability_policy_sha256,
                        "host_control_policy_sha256": self.host_control_policy.sha256,
                        "host_control_catalog_sha256": self.host_control_policy.catalog.sha256,
                        "executor_policy_config_sha256": self.executor_policy_config_sha256,
                        "checkpoint_commit_decision": self.host_control_policy.authorize(
                            "checkpoint_commit", CallerOrigin.HOST_LIFECYCLE
                        ).model_dump(mode="json"),
                        "journal_size": 0, "journal_sha256": hashlib.sha256(b"").hexdigest(),
                        "baseline_sha256": hashlib.sha256(b"fixed test baseline").hexdigest(),
                        "image_id": self.image_id, "container_id": self.container_id,
                        "tool_sha256": tool_identity(self.executor_specs), "test_evidence": {}}
            if jobs:
                artifact_path = directory / f"{batch}.artifacts.tar"
                with artifact_path.open("wb") as stream:
                    receipt = self.artifacts.export_archive(stream)
                snapshot.update(artifacts_enabled=True, streaming_artifacts=True,
                    artifact_run_id=self.job_settings.logical_run_id, run_id=self.root.name,
                    artifact_archive=artifact_path.relative_to(self.root).as_posix(),
                    artifact_archive_bytes=artifact_path.stat().st_size,
                    artifact_archive_sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                    artifact_receipt=receipt, job_protocol=self.job_protocol)
            atomic_json(directory / f"{batch}.json", snapshot)
            return snapshot

        def export_evidence(self):
            pass

        def set_phase(self, phase):
            pass

        def remove_previous_container(self, snapshot):
            assert snapshot["container_id"] != self.container_id

        def close(self, *, remove):
            if jobs:
                assert self.worker.shutdown(worker_epoch=self.job_settings.worker_epoch)["closed"]
            closed.append(self.container_id)

        def bind_job_guard(self, guard):
            self.guard = guard

    monkeypatch.setattr(runtime, "DockerToolBackend", HostBackend)
    original_run = subprocess.run

    def run(command, **kwargs):
        if command[:3] == ["docker", "version", "--format"]:
            return SimpleNamespace(returncode=0, stdout="27.5.1\n", stderr="")
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=image + "\n", stderr="")
        return original_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    original_save = CheckpointStore.save

    def pause(store, payload, **kwargs):
        saved = original_save(store, payload, **kwargs)
        if kwargs["sequence"] == 3 and "agent" in payload:
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
                       harness_foundations_enabled=jobs, background_jobs_enabled=jobs,
                       test_target="tests/test_widget.py", source_import="widget")
    interrupted = runner.run(config)
    assert interrupted.status == "interrupted" and interrupted.step_count == 2
    root = Path(interrupted.result_path).parent
    assert runner.inspect(root)["resumable"]
    monkeypatch.setattr(CheckpointStore, "save", original_save)
    completed = runner.resume(root)
    assert completed.status == "completed" and completed.agent_validation_status == "verified"
    assert completed.error is None
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
    diagnostics = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in root.glob("recovery-failure-*.json")
    ]
    assert {row["stage"] for row in diagnostics} == (
        {"resume", "cleanup"} if cleanup_fails else {"resume"}
    )
    assert all(row["checkpoint_sequence"] == 1 for row in diagnostics)


@pytest.mark.parametrize("tool_failure", [False, True])
def test_disposable_docker_regression_backend_is_always_removed(
    tmp_path, monkeypatch, tool_failure
):
    source = tmp_path / "source"
    source.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = RunConfig(
        repo=source,
        task="validate compatibility",
        execution_backend="docker",
        docker_profile="ordinary",
        docker_image_id="sha256:" + "a" * 64,
        source_import="widget",
        test_target="tests/test_main.py",
        regression_targets=("tests/test_compat.py",),
        docker_recovery_enabled=True,
    )
    events = []

    class RunTool:
        from tracefix.tools.base import ToolSpec

        spec = ToolSpec(name="run_tests", description="baseline fixture")

        def prepare(self, call):
            events.append(("prepare", call.id))

        def execute(self, call):
            events.append(("execute", call.id))
            if tool_failure:
                raise RuntimeError("baseline execution failed")
            return SimpleNamespace(model_dump=lambda mode: {"success": True, "mode": mode})

    class Registry:
        def get(self, name):
            assert name == "run_tests"
            return RunTool()

    class Backend:
        def __init__(self, **kwargs):
            events.append(("init", kwargs["run_id"], kwargs["image_id"]))

        def prepare(self, source_commit, source_repo, package_root, **kwargs):
            events.append(("backend-prepare", source_commit, kwargs["source_import_probe"]))
            return Registry()

        def export_evidence(self):
            events.append(("export",))

        def close(self, *, remove):
            events.append(("close", remove))

    import tracefix.docker_backend as docker_backend

    monkeypatch.setattr(docker_backend, "DockerToolBackend", Backend)
    call = runtime.ToolCall(id="baseline-call", name="run_tests", arguments={})
    if tool_failure:
        with pytest.raises(RuntimeError, match="baseline execution failed"):
            runtime.TraceFixRunner._run_docker_regression_baseline(
                config, source, "saved-commit", run_dir, 2, call
            )
    else:
        result = runtime.TraceFixRunner._run_docker_regression_baseline(
            config, source, "saved-commit", run_dir, 2, call
        )
        assert result == {"success": True, "mode": "json"}
        assert ("export",) in events
    assert events[-1] == ("close", True)


def test_docker_validation_environment_binds_recipe_and_test_environment(tmp_path):
    from tracefix.real_recipes import EnvironmentRecipe
    from tracefix.validation import docker_validation_environment_sha256

    config = RunConfig(
        repo=tmp_path,
        task="validate environment",
        execution_backend="docker",
        docker_profile="ordinary",
        docker_image_id="sha256:" + "b" * 64,
        source_import="widget",
        test_target="tests/test_main.py",
        environment_recipe=EnvironmentRecipe(
            task_id="tracefix-ordinary", environment_variables={"BASE_FLAG": "base"}
        ),
        test_environment_variables={"TEST_FLAG": "test"},
    )

    actual = TraceFixRunner._validation_environment_sha256(config)

    assert actual == docker_validation_environment_sha256(
        config.docker_image_id,
        {"BASE_FLAG": "base", "TEST_FLAG": "test"},
    )
