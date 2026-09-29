"""Ordinary Docker preparation, configuration, and fail-closed lifecycle tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.cli import _ordinary_settings, build_parser
from tracefix.docker_backend import DockerToolBackend
from tracefix.exceptions import WorkspaceError
from tracefix.ordinary_docker import prepare_image
from tracefix.runtime import RunConfig

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
