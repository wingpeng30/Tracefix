"""Prepare contract tests supplement installed-wheel real Linux container probes."""

import json
from types import SimpleNamespace

import pytest

from tracefix import docker_backend
from tracefix.docker_recovery import tool_identity
from tracefix.exceptions import WorkspaceError
from tracefix.tools.base import ToolResult, ToolSpec


@pytest.mark.parametrize("mode", ["off", "on", "restore", "tool", "interpreter", "bridge"])
def test_prepare_binds_recovery_identity_and_only_opted_in_cache_policy(
    tmp_path, monkeypatch, mode,
):
    image = "sha256:" + "a" * 64
    baseline = tmp_path / "agent-base.tar"
    baseline.write_bytes(b"saved baseline")
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "SKILL.md").write_bytes(b"immutable skill snapshot")
    spec = ToolSpec(name="load_skill", description="load", input_schema={"type": "object"})
    interpreter = {"python": "3.11.16", "pytest": "8.4.2"}
    state = {"schema_version": 1, "protected_dirs": [".tracefix-build-tmp"], "skills": {}}
    snapshot = {"tool_sha256": tool_identity([spec]), "interpreter": interpreter,
                "bridge_state": state}
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
            value = image.encode()
        elif command[1] == "inspect":
            formats = {
                "{{.Id}}": "owned-container",
                "{{json .HostConfig.NetworkMode}}": '"none"', "{{json .Mounts}}": "[]",
                "{{.Config.User}}": "10001:10001",
                "{{json .HostConfig}}": json.dumps({"ReadonlyRootfs": True, "PidsLimit": 256,
                    "Memory": 2 * 1024**3, "NanoCpus": 1_000_000_000}),
            }
            value = formats.get(command[command.index("--format") + 1], "run").encode()
        elif "-c" in command and "platform.python_version" in command[-1]:
            value = json.dumps(interpreter).encode()
        elif "-c" in command and "importlib.import_module" in command[-1]:
            value = b'"/work/agent/widget.py"'
        return SimpleNamespace(stdout=value, returncode=0, stderr=b"")

    class Bridge:
        def __init__(self, command, *args, **kwargs):
            assert "--skills-root" in command
            self.run_id = "run"
            self.tools = (spec,)
            self.skill_catalog = ()
            self.repo_map = None

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
    recovering = mode not in {"on", "off"}

    def prepare():
        return backend.prepare("saved-commit", tmp_path, tmp_path, source_import_probe="widget",
            skills_enabled=True, skills_root=skills, baseline_archive=baseline,
            recovery_enabled=mode != "off", recovery_snapshot=snapshot if recovering else None)

    try:
        if mode in {"tool", "interpreter", "bridge"}:
            with pytest.raises(WorkspaceError, match="changed|restore exactly"):
                prepare()
        else:
            registry = prepare()
            assert "load_skill" in registry.names
            assert bool(restored) == recovering
        exclusions = [command for command, _ in commands if ".git/info/exclude" in command[-1]]
        assert bool(exclusions) == (mode != "off")
        assert any("a-w" in command for command, _ in commands)
        assert ("-recovery-" in backend.container_name) == recovering
    finally:
        backend.close(remove=True)
