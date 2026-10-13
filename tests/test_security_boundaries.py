from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tracefix.exceptions import ToolValidationError
from tracefix.mcp_serena import SerenaMCP
from tracefix.messages import ToolCall
from tracefix.repository import RepositoryIndexer
from tracefix.runtime import RunConfig, _saved_run_config
from tracefix.security import minimal_subprocess_environment, validate_project_environment
from tracefix.tools.builtin import (
    ApplyPatchTool,
    GetGitDiffTool,
    ReadFileTool,
    RunTestsTool,
    SearchCodeTool,
)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src.py").write_text("VALUE = 'public'\n", encoding="utf-8")
    (root / ".env.example").write_text("PUBLIC_TEMPLATE=value\n", encoding="utf-8")
    (root / ".env").write_text("SECRET_CANARY_95F2\n", encoding="utf-8")
    (root / ".aws").mkdir()
    (root / ".aws" / "secret.py").write_text("SECRET_CANARY_95F2\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "--all"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=TraceFix", "-c", "user.email=tests@example.invalid",
         "commit", "-qm", "fixture"],
        cwd=root,
        check=True,
    )
    return root


def _call(name: str, arguments: dict) -> ToolCall:
    return ToolCall(id="security-test", name=name, arguments=arguments)


def test_direct_reads_and_bulk_views_hide_tracked_secrets(repository: Path, tmp_path: Path) -> None:
    reader = ReadFileTool(repository)
    with pytest.raises(ToolValidationError, match="secret environment files"):
        reader.execute(_call("read_file", {"path": ".env"}))
    assert reader.execute(_call("read_file", {"path": ".env.example"})).success
    for name in (".git/config", ".aws/secret.py"):
        with pytest.raises(ToolValidationError):
            reader.execute(_call("read_file", {"path": name}))
    patch_tool = ApplyPatchTool(repository)
    rejected_patch = (
        "diff --git a/.aws/secret.py b/.aws/secret.py\n"
        "--- a/.aws/secret.py\n+++ b/.aws/secret.py\n"
        "@@ -1 +1 @@\n-SECRET_CANARY_95F2\n+changed\n"
    )
    with pytest.raises(ToolValidationError):
        patch_tool.execute(_call("apply_patch", {"patch": rejected_patch}))

    search = SearchCodeTool(repository).execute(
        _call("search_code", {"query": "SECRET_CANARY_95F2"})
    )
    assert search.success and search.output["matches"] == []

    index = RepositoryIndexer(repository).build()
    assert all(".aws" not in item.path for item in index.files)

    (repository / ".env").write_text("CHANGED SECRET_CANARY_95F2\n", encoding="utf-8")
    diff = GetGitDiffTool(repository).execute(_call("get_git_diff", {}))
    assert diff.success
    assert "SECRET_CANARY_95F2" not in diff.output["diff"]
    assert ".env" not in diff.output["changed_files"]

    target = tmp_path / "snapshot"
    target.mkdir()
    manager = SerenaMCP(repository, tmp_path, "sha256:" + "a" * 64)
    manager._snapshot(target)
    assert not (target / ".env").exists()
    assert not (target / ".aws" / "secret.py").exists()
    assert "SECRET_CANARY_95F2" not in "\n".join(
        path.read_text(encoding="utf-8")
        for path in target.rglob("*")
        if path.is_file()
    )


def test_real_path_aliases_cannot_reach_dotenv_or_git_metadata(
    repository: Path, tmp_path: Path
) -> None:
    dotenv_alias = repository / "public-name.txt"
    git_alias = repository / "git-config.txt"
    try:
        dotenv_alias.symlink_to(repository / ".env")
        git_alias.symlink_to(repository / ".git" / "config")
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    for alias in (dotenv_alias, git_alias):
        with pytest.raises(ToolValidationError):
            ReadFileTool(repository).execute(
                _call("read_file", {"path": alias.name})
            )
    search = SearchCodeTool(repository).execute(
        _call("search_code", {"query": "SECRET_CANARY_95F2"})
    )
    assert search.output["matches"] == []


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q --deselect tests/test_x.py::test_y tests/test_x.py",
        "pytest -m 'slow and not network' --collect-only tests",
        "pytest --version tests",
        "pytest -p no:cacheprovider --basetemp=.tracefix-test-tmp tests",
    ],
)
def test_pytest_allowlist_keeps_expected_options(command: str) -> None:
    parsed = RunTestsTool._parse_command(command)
    assert parsed[1:3] == ["-m", "pytest"]


@pytest.mark.parametrize(
    "command",
    [
        "pytest ../outside.py",
        "pytest C:\\outside.py",
        "pytest --basetemp=../outside tests",
        "pytest -p injected_plugin tests",
        "pytest -pno:cacheprovider tests",
        "pytest -o addopts=--collect-only tests",
        "pytest --override-ini=addopts= tests",
        "pytest --junitxml=../report.xml tests",
        "pytest --log-file=../pytest.log tests",
    ],
)
def test_pytest_allowlist_rejects_escape_and_control_options(
    repository: Path, tmp_path: Path, command: str
) -> None:
    tool = RunTestsTool(repository, evidence_dir=tmp_path / "evidence")
    with pytest.raises(ToolValidationError):
        tool.prepare(_call("run_tests", {"command": command}))


def test_invalid_pytest_target_is_rejected_before_process_start(
    repository: Path,
) -> None:
    started: list[bool] = []
    tool = RunTestsTool(
        repository,
        evidence_dir=repository.parent / "target-evidence",
        on_process_started=lambda: started.append(True),
    )
    with pytest.raises(ToolValidationError):
        tool.execute(_call("run_tests", {"command": "pytest ../outside.py"}))
    assert started == []


def test_environment_is_minimal_and_explicit_project_values_are_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "safe-path")
    monkeypatch.setenv("PYTHONPATH", "outside-checkout")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "canary")
    monkeypatch.setenv("GITHUB_TOKEN", "canary")
    monkeypatch.setenv("CUSTOM_SECRET", "canary")
    environment = minimal_subprocess_environment()
    assert environment["PATH"] == "safe-path"
    assert not {"PYTHONPATH", "AWS_ACCESS_KEY_ID", "GITHUB_TOKEN", "CUSTOM_SECRET"} & set(
        environment
    )
    assert validate_project_environment({"HTTPBIN_URL": "http://127.0.0.1:8765"})
    for name in ("AWS_ACCESS_KEY_ID", "GITHUB_TOKEN", "PYTEST_ADDOPTS", "PYTHONPATH"):
        with pytest.raises(ValueError):
            validate_project_environment({name: "canary"})


def test_pytest_process_does_not_inherit_credentials_or_pytest_controls(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "credential-canary")
    monkeypatch.setenv("PYTHONPATH", "outside-checkout")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p injected_plugin")
    monkeypatch.setenv("PYTEST_PLUGINS", "injected_plugin")
    tests = repository / "tests"
    tests.mkdir()
    (tests / "test_environment.py").write_text(
        "import os\n"
        "def test_environment_is_controlled():\n"
        "    assert os.getenv('AWS_ACCESS_KEY_ID') is None\n"
        "    assert os.getenv('PYTEST_ADDOPTS') is None\n"
        "    assert os.getenv('PYTEST_PLUGINS') is None\n"
        "    assert os.getenv('SAFE_PROJECT_VALUE') == 'explicit'\n",
        encoding="utf-8",
    )
    original_popen = subprocess.Popen
    captured: dict[str, str] = {}

    def capture_popen(*args, **kwargs):
        if "env" in kwargs:
            captured.update(kwargs["env"])
        return original_popen(*args, **kwargs)

    monkeypatch.setattr("tracefix.tools.builtin.subprocess.Popen", capture_popen)
    tool = RunTestsTool(
        repository,
        environment_variables={"SAFE_PROJECT_VALUE": "explicit"},
        evidence_dir=repository.parent / "evidence",
    )
    result = tool.execute(_call("run_tests", {"command": "pytest -q tests/test_environment.py"}))
    assert result.success, result.error
    assert "AWS_ACCESS_KEY_ID" not in captured
    assert "PYTEST_ADDOPTS" not in captured and "PYTEST_PLUGINS" not in captured
    assert captured["SAFE_PROJECT_VALUE"] == "explicit"
    assert str(repository) in captured["PYTHONPATH"]
    assert "outside-checkout" not in captured["PYTHONPATH"]


def test_build_and_import_probe_environments_drop_inherited_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tracefix.real_environment import _managed_process_environment
    from tracefix.real_experiment import _isolated_build_environment, _probe_source_import
    from tracefix.real_recipes import EnvironmentRecipe

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "credential-canary")
    monkeypatch.setenv("PYTHONPATH", "outside-checkout")
    project_values = {"HTTPBIN_URL": "https://example.invalid"}
    build_root = tmp_path / "build"
    build_root.mkdir()
    build = _isolated_build_environment(build_root, project_values)
    managed = _managed_process_environment(tmp_path / "environment")
    for environment in (build, managed):
        assert "AWS_ACCESS_KEY_ID" not in environment
        assert environment.get("PYTHONPATH") != "outside-checkout"
    assert build["HTTPBIN_URL"] == project_values["HTTPBIN_URL"]

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    module = checkout / "sample.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    recipe = EnvironmentRecipe(
        task_id="fixture-task",
        source_import_probe="sample",
        environment_variables=project_values,
    )
    captured: dict[str, str] = {}

    def fake_run(_command, *, env, **_kwargs):
        captured.update(env)
        return subprocess.CompletedProcess(_command, 0, str(module), "")

    monkeypatch.setattr("tracefix.real_experiment.subprocess.run", fake_run)
    message, imported = _probe_source_import(recipe, checkout, Path("python"))
    assert message is None and imported == str(module)
    assert "AWS_ACCESS_KEY_ID" not in captured
    assert "outside-checkout" not in captured["PYTHONPATH"]
    assert captured["HTTPBIN_URL"] == project_values["HTTPBIN_URL"]


def test_new_config_defaults_to_ordinary_docker_and_saved_legacy_config_stays_local(
    repository: Path,
) -> None:
    config = RunConfig(repo=repository, task="new task")
    assert (config.execution_backend, config.docker_profile) == ("docker", "ordinary")

    local = RunConfig(repo=repository, task="local task", execution_backend="local")
    assert (local.execution_backend, local.docker_profile) == ("local", "frozen")

    saved = _saved_run_config({"repo": str(repository), "task": "old task"})
    assert (saved.execution_backend, saved.docker_profile) == ("local", "frozen")
