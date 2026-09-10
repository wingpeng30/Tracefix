import subprocess
import sys
from pathlib import Path

import pytest

from tracefix import (
    LLMConfig,
    collect_run_provenance,
    inspect_test_environment,
    task_sha256,
)


def _git(repo, *arguments):
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def test_provenance_records_commit_dirty_task_model_and_dependencies(tmp_path) -> None:
    """溯源清单应区分干净/脏工作区，并完整记录比较实验所需参数。"""
    repo = tmp_path / "tracefix-source"
    repo.mkdir()
    (repo / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "--all")
    _git(
        repo,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "initial",
    )
    config = LLMConfig(
        model_name="provider/model",
        temperature=0.2,
        extra_kwargs={"api_key": "credential-value-123"},
    )

    clean = collect_run_provenance("同一个任务", config, project_root=repo)
    assert clean.tracefix_commit == _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert clean.tracefix_worktree_dirty is False
    assert clean.task_sha256 == task_sha256("同一个任务")
    assert clean.model_parameters["temperature"] == 0.2
    assert clean.model_parameters["extra_kwargs"]["api_key"] == "<redacted>"
    assert clean.dependency_versions["pydantic"] is not None

    (repo / "tracked.py").write_text("VALUE = 2\n", encoding="utf-8")
    dirty = collect_run_provenance("同一个任务", config, project_root=repo)
    assert dirty.tracefix_worktree_dirty is True


def test_provenance_handles_install_without_git_metadata(tmp_path) -> None:
    config = LLMConfig(model_name="provider/model")
    provenance = collect_run_provenance("task", config, project_root=tmp_path)
    assert provenance.tracefix_commit is None
    assert provenance.tracefix_worktree_dirty is None


def test_test_environment_fingerprint_includes_pythonpath_artifacts(tmp_path) -> None:
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "sitecustomize.py").write_text("VALUE = 1\n", encoding="utf-8")

    first = inspect_test_environment(
        Path(sys.executable), pythonpath_entries=(bootstrap,)
    )
    (bootstrap / "sitecustomize.py").write_text("VALUE = 2\n", encoding="utf-8")
    second = inspect_test_environment(
        Path(sys.executable), pythonpath_entries=(bootstrap,)
    )

    assert first.python_version
    assert first.dependency_versions["pydantic"]
    assert first.pythonpath_artifacts["bootstrap"] != second.pythonpath_artifacts["bootstrap"]
    assert first.fingerprint_sha256 != second.fingerprint_sha256


def test_test_environment_rejects_missing_interpreter_and_artifact(tmp_path) -> None:
    with pytest.raises(ValueError, match="executable"):
        inspect_test_environment(tmp_path / "missing-python")
    with pytest.raises(ValueError, match="PYTHONPATH"):
        inspect_test_environment(
            Path(sys.executable), pythonpath_entries=(tmp_path / "missing-dir",)
        )
