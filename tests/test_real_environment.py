"""真实任务环境准备器的回归测试。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.exceptions import BenchmarkError
from tracefix.real_benchmark import RealIssueTask
from tracefix.real_environment import (
    EnvironmentPreparationConfig,
    RealEnvironmentPreparer,
    _is_safe_managed_path,
    _link_build_copy_git_metadata,
    _log,
    _looks_incompatible,
    _owner_task_id,
    _run,
    _select_interpreter,
    apply_cleanup,
    discover_interpreters,
    inspect_storage,
    preview_cleanup,
    resolve_managed_environment_python,
)
from tracefix.real_recipes import EnvironmentRecipe, load_environment_recipes


def _digest(path: Path) -> str:
    """计算与真实清单一致的规范化文本摘要。"""
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _task(tmp_path: Path) -> tuple[RealIssueTask, Path]:
    """构造可通过本地 pip 安装的最小固定源码任务。"""
    source = tmp_path / "sources" / "owner__repo-1"
    package = source / "demo_package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "setup.py").write_text(
        "from setuptools import setup\nsetup(name='tracefix-env-fixture', version='0.0.1')\n",
        encoding="utf-8",
    )
    task_dir = tmp_path / "tasks" / "owner__repo-1"
    task_dir.mkdir(parents=True)
    (task_dir / "problem.md").write_text("fixture\n", encoding="utf-8")
    gold = (
        "diff --git a/demo_package/__init__.py b/demo_package/__init__.py\n"
        "--- a/demo_package/__init__.py\n+++ b/demo_package/__init__.py\n"
        "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
    )
    hidden = (
        "diff --git a/tests/test_demo.py b/tests/test_demo.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/tests/test_demo.py\n@@ -0,0 +1 @@\n"
        "+def test_demo(): assert True\n"
    )
    (task_dir / "gold.patch").write_text(gold, encoding="utf-8")
    (task_dir / "test.patch").write_text(hidden, encoding="utf-8")
    manifest = {
        "id": "owner__repo-1",
        "title": "fixture",
        "repo": "owner/repo",
        "repo_url": "https://github.com/owner/repo.git",
        "issue_url": "https://github.com/owner/repo/issues/1",
        "base_commit": "a" * 40,
        "environment_setup_commit": "a" * 40,
        "upstream_version": "1",
        "issue_created_at": "2024-01-01T00:00:00Z",
        "fail_to_pass": ["tests/test_demo.py"],
        "test_command": "pytest -q",
        "pass_to_pass_count": 0,
        "expected_source_files": ["demo_package/__init__.py"],
        "expected_test_files": ["tests/test_demo.py"],
        "related_context_files": ["demo_package/__init__.py"],
        "hashes": {
            "problem_statement": _digest(task_dir / "problem.md"),
            "gold_patch": _digest(task_dir / "gold.patch"),
            "test_patch": _digest(task_dir / "test.patch"),
        },
    }
    (task_dir / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    return RealIssueTask.load(task_dir), source


def test_preparer_creates_and_reuses_isolated_environment(tmp_path, monkeypatch) -> None:
    """可安装任务第一次创建环境，第二次复用同一健康环境。"""
    task, source = _task(tmp_path)
    config = EnvironmentPreparationConfig(
        tasks_dir=task.task_dir.parent,
        source_root=source.parent,
        environment_root=tmp_path / "envs",
        output_dir=tmp_path / "results",
        python_executable=Path(sys.executable),
    )
    preparer = RealEnvironmentPreparer()

    def fake_run(command, cwd, timeout, environment=None):
        if command[2:3] == ("venv",):
            return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("tracefix.real_environment._run", fake_run)
    first = preparer.prepare(config).results[0]
    second = preparer.prepare(config).results[0]

    assert first.status == "ready"
    assert first.environment is not None
    assert second.status == "reused"
    assert Path(first.python_executable or "").is_file()


def test_recipe_hash_invalidates_reuse_and_storage_ignores_unmanaged_paths(
    tmp_path, monkeypatch
) -> None:
    """配方变化必须使旧环境失效，空间统计不能把普通目录视作可清理目标。"""
    task, source = _task(tmp_path)
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    recipe = EnvironmentRecipe(task_id=task.id, python_versions=("3.12",))
    (recipes / "fixture.json").write_text(recipe.model_dump_json(), encoding="utf-8")
    config = EnvironmentPreparationConfig(
        tasks_dir=task.task_dir.parent,
        source_root=source.parent,
        environment_root=tmp_path / "envs",
        output_dir=tmp_path / "results",
        python_executable=Path(sys.executable),
        recipes_dir=recipes,
    )
    preparer = RealEnvironmentPreparer()

    def fake_run(command, cwd, timeout, environment=None):
        if command[2:3] == ("venv",):
            return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("tracefix.real_environment._run", fake_run)
    assert preparer.prepare(config).results[0].status == "ready"
    (tmp_path / "envs" / "ordinary").mkdir()
    report = inspect_storage(tmp_path / "envs")
    preview = preview_cleanup(tmp_path / "envs")
    assert report.managed_environment_bytes > 0
    assert len(preview.paths) == 1

    changed = recipe.model_copy(update={"extra_dependencies": ("demo==1",)})
    (recipes / "fixture.json").write_text(changed.model_dump_json(), encoding="utf-8")
    assert preparer.prepare(config).results[0].status == "ready"


def test_preparer_runs_recipe_build_in_isolated_copy(tmp_path, monkeypatch) -> None:
    """历史版本文件生成只能在环境构建副本中执行，并记录到安装命令。"""
    task, source = _task(tmp_path)
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    recipe = EnvironmentRecipe(
        task_id=task.id, build_commands=(("{python}", "setup.py", "--version"),)
    )
    (recipes / "fixture.json").write_text(recipe.model_dump_json(), encoding="utf-8")
    config = EnvironmentPreparationConfig(
        tasks_dir=task.task_dir.parent,
        source_root=source.parent,
        environment_root=tmp_path / "envs",
        output_dir=tmp_path / "results",
        python_executable=Path(sys.executable),
        recipes_dir=recipes,
    )
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd, timeout, environment=None):
        commands.append(command)
        if command[2:3] == ("venv",):
            return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("tracefix.real_environment._run", fake_run)
    result = RealEnvironmentPreparer().prepare(config).results[0]
    assert result.status == "ready"
    assert any(command[-2:] == ("setup.py", "--version") for command in commands)


def test_preparer_keeps_git_metadata_only_in_build_copy(tmp_path, monkeypatch) -> None:
    """需要 setuptools-scm 的构建副本保留 Git 元数据，固定源码仍不被写入。"""
    task, source = _task(tmp_path)
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    config = EnvironmentPreparationConfig(
        tasks_dir=task.task_dir.parent,
        source_root=source.parent,
        environment_root=tmp_path / "envs",
        output_dir=tmp_path / "results",
        python_executable=Path(sys.executable),
    )
    copied_git_metadata: list[str] = []

    def fake_run(command, cwd, timeout, environment=None):
        if command[2:3] == ("venv",):
            return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
        if command[-1:] == (".",):
            copied_git_metadata.append((cwd / ".git").read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("tracefix.real_environment._run", fake_run)
    assert RealEnvironmentPreparer().prepare(config).results[0].status == "ready"
    assert copied_git_metadata == [f"gitdir: {(source / '.git').resolve()}\n"]
    assert (source / ".git" / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main\n"


def test_preparer_records_failed_recipe_build_without_installing_project(
    tmp_path, monkeypatch
) -> None:
    """构建步骤失败必须保留诊断并停止项目安装，不能产生健康环境标记。"""
    task, source = _task(tmp_path)
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    recipe = EnvironmentRecipe(
        task_id=task.id, build_commands=(("{python}", "setup.py", "--version"),)
    )
    (recipes / "fixture.json").write_text(recipe.model_dump_json(), encoding="utf-8")
    config = EnvironmentPreparationConfig(
        tasks_dir=task.task_dir.parent,
        source_root=source.parent,
        environment_root=tmp_path / "envs",
        output_dir=tmp_path / "results",
        python_executable=Path(sys.executable),
        recipes_dir=recipes,
    )

    def fake_run(command, cwd, timeout, environment=None):
        if command[2:3] == ("venv",):
            return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
        if command[-2:] == ("setup.py", "--version"):
            return subprocess.CompletedProcess(command, 1, "", "version failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("tracefix.real_environment._run", fake_run)
    result = RealEnvironmentPreparer().prepare(config).results[0]
    assert result.status == "install_failed"
    assert any("recipe_build_0" in log for log in result.logs)
    assert not (tmp_path / "envs" / task.id / ".tracefix-environment.json").exists()


def test_build_copy_accepts_gitfile_metadata(tmp_path) -> None:
    """worktree 形式的固定源码也能把 gitdir 文件原样带入构建副本。"""
    source = tmp_path / "source"
    build = tmp_path / "build"
    source.mkdir()
    build.mkdir()
    (source / ".git").write_text("gitdir: D:/metadata\n", encoding="utf-8")
    _link_build_copy_git_metadata(source, build)
    assert (build / ".git").read_text(encoding="utf-8") == "gitdir: D:/metadata\n"


def test_recipe_loader_rejects_undocumented_selector_override(tmp_path) -> None:
    """测试入口替代必须写清缘由，避免悄悄缩小官方验收范围。"""
    (tmp_path / "bad.json").write_text(
        json.dumps({"task_id": "owner__repo-1", "selector_overrides": ["tests/test_x.py"]}),
        encoding="utf-8",
    )
    try:
        load_environment_recipes(tmp_path)
    except Exception as exc:
        assert "invalid environment recipe" in str(exc)
    else:
        raise AssertionError("invalid recipe should be rejected")


@pytest.mark.parametrize("command", [(), ("python", "setup.py")])
def test_recipe_rejects_unsafe_build_commands(command) -> None:
    """构建步骤必须非空且只能通过已选择的解释器启动。"""
    with pytest.raises(ValueError):
        EnvironmentRecipe(task_id="owner__repo-1", build_commands=(command,))


def test_recipe_loader_rejects_duplicate_task_ids(tmp_path) -> None:
    recipe = EnvironmentRecipe(task_id="owner__repo-1")
    (tmp_path / "a.json").write_text(recipe.model_dump_json(), encoding="utf-8")
    (tmp_path / "b.json").write_text(recipe.model_dump_json(), encoding="utf-8")
    with pytest.raises(Exception, match="duplicate environment recipe"):
        load_environment_recipes(tmp_path)


def test_preparer_preserves_unregistered_or_unhealthy_environment(tmp_path, monkeypatch) -> None:
    """旧目录缺少 TraceFix 所有权或健康标记时必须保留，并改用新目录。"""
    task, source = _task(tmp_path)
    legacy = tmp_path / "envs" / task.id
    legacy.mkdir(parents=True)
    (legacy / "user-note.txt").write_text("keep", encoding="utf-8")
    config = EnvironmentPreparationConfig(
        tasks_dir=task.task_dir.parent,
        source_root=source.parent,
        environment_root=tmp_path / "envs",
        output_dir=tmp_path / "results",
        python_executable=Path(sys.executable),
    )

    def fake_run(command, cwd, timeout, environment=None):
        if command[2:3] == ("venv",):
            return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("tracefix.real_environment._run", fake_run)
    result = RealEnvironmentPreparer().prepare(config).results[0]
    assert result.status == "ready"
    assert (legacy / "user-note.txt").read_text(encoding="utf-8") == "keep"
    assert task.id + "--rebuild-" in str(result.python_executable)


def test_managed_path_rejects_escape_and_link(tmp_path) -> None:
    """路径必须同时通过原始路径、解析边界和链接检查。"""
    root = tmp_path / "managed"
    root.mkdir()
    assert not _is_safe_managed_path(root, root / ".." / "outside")
    assert not _is_safe_managed_path(root, root)
    link = root / "junction"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        return
    assert not _is_safe_managed_path(root, link / "child")


def test_environment_resolver_finds_only_registered_rebuild(tmp_path, monkeypatch) -> None:
    """CLI 与实验器应找到重建环境，并忽略碰巧同名的未登记目录。"""
    root = tmp_path / "envs"
    root.mkdir()
    (root / "owner__repo-1").mkdir()
    rebuilt = root / "owner__repo-1--rebuild-0001"
    python = rebuilt / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    (rebuilt / ".tracefix-owner.json").write_text(
        json.dumps(
            {
                "managed_kind": "environment",
                "task_id": "owner__repo-1",
                "environment_root": str(root.resolve()),
                "environment_path": str(rebuilt.resolve()),
            }
        ),
        encoding="utf-8",
    )
    (rebuilt / ".tracefix-environment.json").write_text(
        json.dumps(
            {
                "task_id": "owner__repo-1",
                "managed_kind": "environment",
                "dependency_fingerprint": "healthy",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "tracefix.real_environment.inspect_test_environment",
        lambda _python: SimpleNamespace(fingerprint_sha256="healthy"),
    )
    assert resolve_managed_environment_python(root, "owner__repo-1") == python


def test_environment_resolver_rejects_stale_registered_environment(tmp_path, monkeypatch) -> None:
    """运行入口不可因只存在所有权标记就复用依赖已漂移的环境。"""
    root = tmp_path / "envs"
    root.mkdir()
    environment = root / "owner__repo-1"
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    (environment / ".tracefix-owner.json").write_text(
        json.dumps(
            {
                "managed_kind": "environment",
                "task_id": "owner__repo-1",
                "environment_root": str(root.resolve()),
                "environment_path": str(environment.resolve()),
            }
        ),
        encoding="utf-8",
    )
    (environment / ".tracefix-environment.json").write_text(
        json.dumps(
            {
                "task_id": "owner__repo-1",
                "managed_kind": "environment",
                "dependency_fingerprint": "old",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "tracefix.real_environment.inspect_test_environment",
        lambda _python: SimpleNamespace(fingerprint_sha256="new"),
    )
    with pytest.raises(BenchmarkError, match="registered environment"):
        resolve_managed_environment_python(root, "owner__repo-1")


def test_environment_helpers_classify_process_and_interpreters(tmp_path, monkeypatch) -> None:
    """环境诊断应保留异常、版本选择和安装不兼容的可审计分类。"""
    monkeypatch.setattr(
        "tracefix.real_environment.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")),
    )
    failed = _run(("missing",), tmp_path, 30)
    assert failed.returncode == 1
    assert "denied" in _log("probe", failed)
    assert _looks_incompatible("Package REQUIRES-PYTHON >= 4")
    assert _looks_incompatible("requires a different python")
    assert not _looks_incompatible("network unavailable")

    monkeypatch.setattr(
        "tracefix.real_environment._run",
        lambda command, *_args: subprocess.CompletedProcess(
            command, 0, "Python 3.10.9" if command[0] != "py" else "", ""
        ),
    )
    explicit = tmp_path / "python.exe"
    records = discover_interpreters(tmp_path, explicit=(explicit, None))
    assert any(item.source == "explicit" and item.version == "3.10.9" for item in records)
    recipe = EnvironmentRecipe(task_id="owner__repo-1", python_versions=("3.10",))
    assert _select_interpreter(recipe, explicit, records).version == "3.10.9"
    assert _select_interpreter(recipe, None, records).version == "3.10.9"
    assert _select_interpreter(recipe, tmp_path / "other.exe", records) is None


def test_cleanup_accepts_registered_environment_and_rejects_bad_owner(tmp_path) -> None:
    """显式清理仅删除所有权完整的环境，伪标记不进入预览。"""
    root = tmp_path / "envs"
    root.mkdir()
    managed = root / "owner__repo-1"
    managed.mkdir()
    (managed / ".tracefix-owner.json").write_text(
        json.dumps(
            {
                "managed_kind": "environment",
                "task_id": "owner__repo-1",
                "environment_root": str(root.resolve()),
                "environment_path": str(managed.resolve()),
            }
        ),
        encoding="utf-8",
    )
    (managed / ".tracefix-environment.json").write_text("{}", encoding="utf-8")
    bad = root / "bad"
    bad.mkdir()
    (bad / ".tracefix-owner.json").write_text("not-json", encoding="utf-8")
    (bad / ".tracefix-environment.json").write_text("{}", encoding="utf-8")
    assert _owner_task_id(bad) == ""
    preview = preview_cleanup(root)
    assert preview.paths == (str(managed.resolve()),)
    applied = apply_cleanup(root)
    assert applied.paths == preview.paths
    assert not managed.exists()
    assert bad.exists()
