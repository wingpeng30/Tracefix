"""Ordinary Docker preparation, configuration, and fail-closed lifecycle tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.cli import _ordinary_settings, build_parser
from tracefix.docker_backend import DockerToolBackend
from tracefix.exceptions import WorkspaceError
from tracefix.onboarding import doctor
from tracefix.ordinary_docker import prepare_image, verify_docker_patch
from tracefix.runtime import RunConfig
from tracefix.tools.base import ToolResult

_IMAGE_ID = "sha256:" + "a" * 64


def test_ordinary_docker_requires_explicit_image_source_and_test(tmp_path):
    base = {
        "repo": tmp_path, "task": "fix", "execution_backend": "docker",
        "docker_profile": "ordinary", "env_file": None,
    }
    with pytest.raises(ValueError, match="image ID"):
        RunConfig(**base)
    valid = RunConfig(
        **base, docker_image_id=_IMAGE_ID, test_target="tests/test_example.py",
        source_import="sample",
    )
    assert valid.docker_task_id is None
    with pytest.raises(ValueError, match="frozen task ID"):
        RunConfig(**{
            **base, "docker_image_id": _IMAGE_ID,
            "test_target": "tests/test_example.py", "source_import": "sample",
            "docker_task_id": "pytest-dev__pytest-10081",
        })
    with pytest.raises(ValueError, match="custom Skills"):
        RunConfig(**{
            **base, "docker_image_id": _IMAGE_ID,
            "test_target": "tests/test_example.py", "source_import": "sample",
            "skills_root": tmp_path,
        })


def test_ordinary_docker_toml_and_cli_image_precedence(tmp_path):
    config = tmp_path / "tracefix.toml"
    config.write_text(
        '[run]\nrepo = "."\ntask = "fix"\n'
        'execution_backend = "docker"\ndocker_profile = "ordinary"\n'
        f'docker_image_id = "{_IMAGE_ID}"\n', encoding="utf-8",
    )
    args = build_parser().parse_args(["run", "--config", str(config)])
    settings = _ordinary_settings(args)
    assert settings["execution_backend"] == "docker"
    assert settings["docker_profile"] == "ordinary"
    assert settings["docker_image_id"] == _IMAGE_ID
    newer = "sha256:" + "b" * 64
    args = build_parser().parse_args([
        "run", "--config", str(config), "--docker-image-id", newer,
    ])
    assert _ordinary_settings(args)["docker_image_id"] == newer


def test_image_preparation_records_exact_inputs_and_probe(tmp_path, monkeypatch):
    requirements = tmp_path / "project.lock"
    requirements.write_text("# reviewed empty dependency lock\n", encoding="utf-8")
    seen = []
    monkeypatch.setattr("tracefix.ordinary_docker.shutil.which", lambda _name: "docker")

    def fake_run(command, **_kwargs):
        seen.append(command)
        if command[1] == "build":
            context = Path(command[-1])
            assert (context / "Dockerfile").is_file()
            assert (context / "ordinary-base.lock").is_file()
            assert (context / "project-requirements.lock").read_bytes() == requirements.read_bytes()
            return SimpleNamespace(returncode=0, stdout="built\n", stderr="")
        if command[1] == "image":
            return SimpleNamespace(returncode=0, stdout=_IMAGE_ID + "\n", stderr="")
        assert command[1] == "run" and "--network" in command
        return SimpleNamespace(returncode=0, stdout="3.11.16\n8.4.2\n", stderr="")

    monkeypatch.setattr("tracefix.ordinary_docker.subprocess.run", fake_run)
    manifest = tmp_path / "prepared.json"
    report = prepare_image(requirements, manifest)
    assert report["image_id"] == _IMAGE_ID
    assert report["pytest_version"] == "8.4.2"
    assert json.loads(manifest.read_text(encoding="utf-8")) == report
    assert len(seen) == 3
    assert Path(report["build_log"]).read_text(encoding="utf-8") == "built\n"
    with pytest.raises(ValueError, match="already exists"):
        prepare_image(requirements, manifest)


def test_image_preparation_keeps_build_failure_log(tmp_path, monkeypatch):
    monkeypatch.setattr("tracefix.ordinary_docker.shutil.which", lambda _name: "docker")
    monkeypatch.setattr(
        "tracefix.ordinary_docker.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="dependency hash mismatch"
        ),
    )
    manifest = tmp_path / "failed.json"
    with pytest.raises(ValueError, match="build failed"):
        prepare_image(None, manifest)
    assert not manifest.exists()
    assert "dependency hash mismatch" in manifest.with_suffix(".build.log").read_text()


@pytest.mark.parametrize("failure", ["missing_cli", "invalid_image", "failed_probe"])
def test_image_preparation_blocks_unusable_runtime(tmp_path, monkeypatch, failure):
    monkeypatch.setattr("tracefix.ordinary_docker.shutil.which", lambda _name: (
        None if failure == "missing_cli" else "docker"
    ))

    def fake_run(command, **_kwargs):
        if command[1] == "build":
            return SimpleNamespace(returncode=0, stdout="built", stderr="")
        if command[1] == "image":
            return SimpleNamespace(returncode=0, stdout=(
                "mutable-tag" if failure == "invalid_image" else _IMAGE_ID
            ), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="pytest unavailable")

    monkeypatch.setattr("tracefix.ordinary_docker.subprocess.run", fake_run)
    manifest = tmp_path / "unusable.json"
    with pytest.raises(ValueError, match={
        "missing_cli": "Docker CLI", "invalid_image": "image inspection",
        "failed_probe": "image preflight",
    }[failure]):
        prepare_image(None, manifest)
    assert not manifest.exists()


def test_ordinary_docker_refuses_wrong_image_and_reports_cleanup_failure(tmp_path, monkeypatch):
    with pytest.raises(Exception, match="fixed image ID"):
        DockerToolBackend(
            task_id="tracefix-ordinary", input_root=tmp_path, run_dir=tmp_path,
            run_id="run", profile="ordinary", image_id="mutable",
        )
    backend = DockerToolBackend(
        task_id="tracefix-ordinary", input_root=tmp_path, run_dir=tmp_path,
        run_id="run", profile="ordinary", image_id=_IMAGE_ID,
    )
    backend.container_id = "owned-container"

    def failed_rm(command, **_kwargs):
        if "inspect" in command:
            return SimpleNamespace(stdout=b"run\n")
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("tracefix.docker_backend._run", failed_rm)
    with pytest.raises(WorkspaceError, match="cleanup was incomplete") as error:
        backend.close(remove=True)
    assert error.value.context["container_id"] == "owned-container"


def test_read_only_container_uses_exec_stream_for_input(tmp_path, monkeypatch):
    backend = DockerToolBackend(
        task_id="tracefix-ordinary", input_root=tmp_path, run_dir=tmp_path,
        run_id="run", profile="ordinary", image_id=_IMAGE_ID,
    )
    source = tmp_path / "source.tar"
    source.write_bytes(b"source archive")
    calls = []
    monkeypatch.setattr("tracefix.docker_backend._run", lambda *args, **kwargs: calls.append(
        (args[0], kwargs)
    ))
    backend._copy_file_into(source, "/input/source.tar")
    assert calls == [
        (["docker", "exec", "-i", backend.container_name, "sh", "-c",
          "cat > /input/source.tar"],
         {"input_data": b"source archive", "timeout": 120}),
    ]


def test_independent_docker_validation_binds_patch_source_image_and_test(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "widget.py").write_text("def next_page(page): return page\n", encoding="utf-8")
    for args in (("init", "-q"), ("add", "--all"),
                 ("-c", "user.name=Test", "-c", "user.email=test@invalid.local",
                  "commit", "-qm", "base")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = RunConfig(
        repo=repo, task="fix", execution_backend="docker", docker_profile="ordinary",
        docker_image_id=_IMAGE_ID, test_target="tests/test_widget.py", source_import="widget",
        env_file=None,
    )
    manifest = {"identity": {"source_commit": commit}}
    patch = b"diff --git a/widget.py b/widget.py\n"
    result = {
        "source_commit": commit,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "workspace_preparation": {
            "profile": "ordinary", "image_id": _IMAGE_ID,
            "test_target": "tests/test_widget.py",
        },
    }
    calls = []
    failure = {"apply": False}

    class FakeTool:
        def __init__(self, name):
            self.name = name

        def execute(self, call):
            calls.append(call.name)
            if call.name == "apply_patch" and failure["apply"]:
                return ToolResult(
                    call_id=call.id, tool_name=call.name,
                    success=False, error="patch rejected",
                )
            return ToolResult(
                call_id=call.id, tool_name=call.name, success=True,
                output={"test_status": "passed"} if call.name == "run_tests" else {},
            )

    class FakeBackend:
        def __init__(self, **kwargs):
            assert kwargs["image_id"] == _IMAGE_ID
            self.workspace_preparation = {"container_id": "new-container"}

        def prepare(self, *_args, **kwargs):
            assert kwargs["source_import_probe"] == "widget"
            return self

        def get(self, name):
            return FakeTool(name)

        def export_evidence(self):
            calls.append("export_evidence")

        def close(self, *, remove):
            assert remove
            calls.append("close")

    monkeypatch.setattr("tracefix.ordinary_docker.DockerToolBackend", FakeBackend)
    record = verify_docker_patch(run_dir, config, manifest, result, patch)
    assert record["passed"] is True
    assert record["image_id"] == _IMAGE_ID
    assert calls == ["apply_patch", "run_tests", "export_evidence", "close"]
    assert json.loads((run_dir / "independent-validation.json").read_text())["passed"]
    with pytest.raises(ValueError, match="already exists"):
        verify_docker_patch(run_dir, config, manifest, result, patch)
    (run_dir / "independent-validation.json").unlink()
    with pytest.raises(ValueError, match="image identity"):
        verify_docker_patch(run_dir, config, manifest, {
            **result, "workspace_preparation": {**result["workspace_preparation"],
                                                "image_id": "sha256:" + "b" * 64},
        }, patch)
    with pytest.raises(ValueError, match="patch is empty"):
        verify_docker_patch(run_dir, config, manifest, result, b"")
    with pytest.raises(ValueError, match="test target changed"):
        verify_docker_patch(run_dir, config, manifest, {
            **result, "workspace_preparation": {
                **result["workspace_preparation"], "test_target": "tests/other.py",
            },
        }, patch)
    with pytest.raises(ValueError, match="source commit"):
        verify_docker_patch(run_dir, config, manifest, {
            **result, "source_commit": "0" * 40,
        }, patch)
    failure["apply"] = True
    with pytest.raises(ValueError, match="patch could not be applied"):
        verify_docker_patch(run_dir, config, manifest, result, patch)
    assert not (run_dir / "independent-validation.json").exists()
    assert calls[-1] == "close"


def test_ordinary_backend_prepares_isolated_bridge_and_source_probe(tmp_path, monkeypatch):
    import tracefix.docker_backend as module

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "widget.py").write_text("value = 1\n", encoding="utf-8")
    for args in (("init", "-q"), ("add", "--all"),
                 ("-c", "user.name=Test", "-c", "user.email=test@invalid.local",
                  "commit", "-qm", "base")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls = []
    original_run = module._run

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "git" and "archive" in command:
            return original_run(command, **kwargs)
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(stdout=(_IMAGE_ID + "\n").encode())
        if command[:2] == ["docker", "inspect"]:
            format_string = command[command.index("--format") + 1]
            if format_string == "{{.Id}}":
                value = "new-container\n"
            elif "NetworkMode" in format_string:
                value = '"none"\n'
            elif ".Mounts" in format_string:
                value = "[]\n"
            elif ".HostConfig" in format_string:
                value = json.dumps({"ReadonlyRootfs": True, "PidsLimit": 256,
                                    "Memory": 2 * 1024**3, "NanoCpus": 1_000_000_000})
            elif format_string == "{{.Config.User}}":
                value = "10001:10001\n"
            else:
                value = "ordinary-run\n"
            return SimpleNamespace(stdout=value.encode())
        if command[:2] == ["docker", "exec"] and "-c" in command:
            script = command[-1]
            if "platform.python_version" in script:
                return SimpleNamespace(stdout=b'{"python":"3.11.16","pytest":"8.4.2"}')
            if "importlib.import_module" in script:
                return SimpleNamespace(stdout=b'"/work/agent/widget.py"')
        return SimpleNamespace(stdout=b"")

    class Bridge:
        def __init__(self, *_args, **_kwargs):
            self.run_id = "ordinary-run"
            self.tools = ()
            self.skill_catalog = ()
            self.repo_map = None

        def close(self):
            pass

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(module, "_BridgeSession", Bridge)
    backend = DockerToolBackend(
        task_id="tracefix-ordinary", input_root=tmp_path,
        run_dir=run_dir, run_id="ordinary-run", profile="ordinary", image_id=_IMAGE_ID,
    )
    backend.prepare(commit, repo, tmp_path, source_import_probe="widget")
    assert backend.workspace_preparation["profile"] == "ordinary"
    assert backend.workspace_preparation["source_import_probe"]["valid"] is True
    assert backend.workspace_preparation["container_interpreter"]["pytest"] == "8.4.2"
    create = next(command for command, _ in calls if command[:2] == ["docker", "create"])
    assert "--read-only" in create and "--user" in create and "--cpus" in create
    assert all(command[1] != "cp" for command, _ in calls if command[0] == "docker")
    backend.close(remove=True)
    assert any(command[:3] == ["docker", "rm", "-f"] for command, _ in calls)


def test_ordinary_doctor_uses_image_and_disposable_container(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "widget.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_widget.py").write_text("def test_ok(): pass\n")
    for args in (("init", "-q"), ("add", "--all"),
                 ("-c", "user.name=Test", "-c", "user.email=test@invalid.local",
                  "commit", "-qm", "base")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    import tracefix.onboarding as module

    original_which = module.shutil.which
    monkeypatch.setattr(module.shutil, "which", lambda name: (
        "docker" if name == "docker" else original_which(name)
    ))
    original_run = subprocess.run

    def fake_run(command, **kwargs):
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=_IMAGE_ID + "\n")
        return original_run(command, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    closed = []

    class Backend:
        def __init__(self, **kwargs):
            assert kwargs["image_id"] == _IMAGE_ID
            self.workspace_preparation = {"success": True,
                                          "source_import_probe": {"valid": True}}

        def prepare(self, *_args, **kwargs):
            assert kwargs["source_import_probe"] == "widget"

        def close(self, *, remove):
            closed.append(remove)

    monkeypatch.setattr("tracefix.docker_backend.DockerToolBackend", Backend)
    settings = {
        "repo": repo, "execution_backend": "docker", "docker_profile": "ordinary",
        "docker_image_id": _IMAGE_ID, "test_python_executable": tmp_path / "absent-python",
        "test_target": "tests/test_widget.py", "source_import": "widget",
        "skills_root": None, "output_dir": tmp_path / "runs",
        "model_name": "offline/replay", "env_file": None,
    }
    outcome = doctor(settings, prepare=True)
    assert outcome["ok"] is True
    assert closed == [True]
    assert not any(item["name"] == "test_python" for item in outcome["checks"])

    def missing_import(*_args, **_kwargs):
        raise WorkspaceError(
            "Docker backend command failed",
            context={"stderr": "ModuleNotFoundError: No module named 'widget'"},
        )

    monkeypatch.setattr(Backend, "prepare", missing_import)
    failed = doctor(settings, prepare=True)
    assert failed["ok"] is False
    assert closed == [True, True]
    assert any(
        "ModuleNotFoundError" in item["detail"]
        for item in failed["checks"] if item["name"] == "prepared_checkout"
    )
