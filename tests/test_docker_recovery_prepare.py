"""Prepare contract tests supplement installed-wheel real Linux container probes."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tracefix import docker_backend
from tracefix.capabilities import CapabilityCatalog, CapabilityPolicy, ExecutionProfile
from tracefix.docker_recovery import tool_identity
from tracefix.exceptions import WorkspaceError
from tracefix.tools.base import ToolResult, ToolSpec


@pytest.mark.parametrize(
    "mode",
    [
        "off", "on", "restore", "tool", "interpreter", "bridge",
        "missing_image", "root_owner", "executor_identity", "writable_executor",
        "jobs", "jobs_bad_init", "jobs_bad_context",
    ],
)
def test_prepare_binds_recovery_identity_and_only_opted_in_cache_policy(
    tmp_path, monkeypatch, mode,
):
    jobs = mode.startswith("jobs")
    image = "sha256:" + "a" * 64
    baseline = tmp_path / "agent-base.tar"
    baseline.write_bytes(b"saved baseline")
    skills = tmp_path / "skills"
    skills.mkdir()
    skill = skills / "tracefix-debugging"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: tracefix-debugging\ndescription: Diagnose TraceFix\n"
        "---\n\nRead the current source first.\n",
        encoding="utf-8",
    )
    spec = ToolSpec(name="load_skill", description="load", input_schema={"type": "object"})
    interpreter = {"python": "3.11.16", "pytest": "8.4.2"}
    state = {"schema_version": 1, "protected_dirs": [".tracefix-build-tmp"], "skills": {}}
    from tracefix.jobs.constants import JOB_CONTROL_CAPABILITIES
    from tracefix.jobs.protocol import JobWorkerSettings

    settings = JobWorkerSettings(logical_run_id="run", worker_epoch="a" * 32,
        host_owner_epoch="b" * 32, session_deadline=datetime.now(UTC) + timedelta(seconds=600))
    catalog = CapabilityCatalog.for_tool_names([
        "load_skill", *(JOB_CONTROL_CAPABILITIES if jobs else ())])
    policy = CapabilityPolicy(catalog, ExecutionProfile.WORKSPACE_WRITE)
    snapshot = {"tool_sha256": tool_identity([spec]), "interpreter": interpreter,
                "bridge_state": state,
                "capability_catalog_sha256": catalog.sha256,
                "capability_policy_sha256": policy.sha256}
    if mode == "tool":
        snapshot["tool_sha256"] = "different"
    if mode == "interpreter":
        snapshot["interpreter"] = {"python": "different", "pytest": "8.4.2"}
    commands = []
    restored = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        value = b""
        if command[1:3] == ["image", "inspect"]:
            value = ("sha256:" + "b" * 64 if mode == "missing_image" else image).encode()
        elif command[1] == "inspect":
            formats = {
                "{{.Id}}": "owned-container",
                "{{json .HostConfig.NetworkMode}}": '"none"', "{{json .Mounts}}": "[]",
                "{{.Config.User}}": "10001:10001",
                    "{{json .HostConfig}}": json.dumps({
                        **({"Init": mode != "jobs_bad_init"} if jobs else {}),
                        "ReadonlyRootfs": True, "PidsLimit": 256,
                        "Memory": 2 * 1024**3, "NanoCpus": 1_000_000_000,
                        "Tmpfs": {"/opt/tracefix": (
                            "rw,uid=10001,gid=10001,mode=755"
                            if mode == "root_owner" else "rw,uid=0,gid=0,mode=755"
                        )},
                    }),
            }
            value = formats.get(command[command.index("--format") + 1], "run").encode()
        elif "-c" in command:
            script = command[command.index("-c") + 1]
            if "capture_idle_tokens" in script:
                value = json.dumps({} if mode == "jobs_bad_context" else {
                    "contract": "tracefix-docker-control-v1", "tokens": [[1, "100"], [7, "101"]]
                }).encode()
            elif "platform.python_version" in script:
                value = json.dumps(interpreter).encode()
            elif "importlib.import_module" in script:
                value = b'"/work/agent/widget.py"'
            elif "manifest=json.loads" in script:
                    value = (
                        b"identity-mismatch" if mode == "executor_identity"
                        else command[-1].encode()
                    )
            elif "p.write_text('probe')" in script:
                    value = b"writable" if mode == "writable_executor" else b"blocked"
        return SimpleNamespace(stdout=value, returncode=0, stderr=b"")

    class Bridge:
        def __init__(self, command, *args, **kwargs):
            assert "--skills-root" in command
            self.run_id = "run"
            self.tools = (spec,)
            self.skill_catalog = ()
            self.repo_map = None
            self.capability_catalog_sha256 = catalog.sha256
            self.capability_policy_sha256 = policy.sha256
            self.executor_policy_config_sha256 = backend.executor_policy_config_sha256
            if jobs:
                assert "--jobs-context-file" in command and kwargs["job_protocol"]
                self.job_protocol = kwargs["job_protocol"]

        def call(self, name, arguments):
            assert name == "__restore_state__" and arguments == state
            restored.append(arguments)
            return ToolResult(call_id="restore", tool_name=name, success=True,
                              output={} if mode == "bridge" else state)

        def close(self):
            pass

    monkeypatch.setattr(docker_backend, "_run", run)
    monkeypatch.setattr(docker_backend, "_BridgeSession", Bridge)
    backend = docker_backend.DockerToolBackend(task_id="tracefix-ordinary", input_root=tmp_path,
        run_dir=tmp_path, run_id="run", profile="ordinary", image_id=image)
    monkeypatch.setattr(backend, "_copy_file_into", lambda *args: None)
    monkeypatch.setattr(backend, "restore_snapshot", lambda saved: None)
    recovering = mode not in {"on", "off"} and not jobs

    def prepare():
        return backend.prepare("saved-commit", tmp_path, tmp_path, source_import_probe="widget",
            skills_enabled=True, skills_root=skills, baseline_archive=baseline,
            recovery_enabled=mode != "off", recovery_snapshot=snapshot if recovering else None,
            **({"job_settings": settings, "artifacts_enabled": True} if jobs else {}))

    try:
        expected_failures = {
            "missing_image": "image identity mismatch",
            "root_owner": "not root-owned",
            "executor_identity": "identity verification failed",
            "writable_executor": "can modify the trusted executor",
            "jobs_bad_init": "verified init process",
            "jobs_bad_context": "control identity capture failed",
        }
        if mode in expected_failures:
            with pytest.raises(WorkspaceError, match=expected_failures[mode]):
                prepare()
            if mode == "missing_image":
                assert not any(command[1] == "create" for command, _ in commands)
            elif mode == "root_owner":
                assert not any("chmod" in command for command, _ in commands)
            elif mode == "executor_identity":
                assert not any("p.write_text('probe')" in str(command) for command, _ in commands)
            elif mode == "writable_executor":
                assert any("p.write_text('probe')" in str(command) for command, _ in commands)
            return
        if mode in {"tool", "interpreter", "bridge"}:
            with pytest.raises(WorkspaceError, match="changed|restore exactly"):
                prepare()
        else:
            registry = prepare()
            assert "load_skill" in registry.names
            assert bool(restored) == recovering
            if jobs:
                create = next(command for command, _ in commands if command[1] == "create")
                assert "--init" in create and create[-1] == "infinity"
                assert create[create.index("--entrypoint") + 1] == "sleep"
                assert backend.job_context.fixed_control_tokens == ((1, "100"), (7, "101"))
                assert backend.job_protocol == backend.session.job_protocol
                programs = [command[command.index("-c") + 1] for command, _ in commands
                            if "-c" in command]
                assert any("finally:" in code and "os.chmod(directory,0o555)" in code
                           and "O_EXCL|os.O_NOFOLLOW" in code for code in programs)
        exclusions = [command for command, _ in commands if ".git/info/exclude" in command[-1]]
        assert bool(exclusions) == (mode != "off")
        root_install = [
            command for command, _ in commands
            if "--user" in command and command[command.index("--user") + 1] == "0:0"
            and "chmod" in command
        ]
        assert any("0444" in command for command in root_install)
        assert any("0555" in command for command in root_install)
        write_probes = [
            command for command, _ in commands
            if any("p.write_text('probe')" in value for value in command)
        ]
        assert len(write_probes) == 1
        assert write_probes[0][write_probes[0].index("--user") + 1] == "10001:10001"
        assert backend.executor_write_probe == "blocked"
        assert ("-recovery-" in backend.container_name) == recovering
    finally:
        backend.close(remove=True)
