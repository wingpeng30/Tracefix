"""真实任务环境准备器的回归测试。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from tracefix.real_benchmark import RealIssueTask
from tracefix.real_environment import (
    EnvironmentPreparationConfig,
    RealEnvironmentPreparer,
    inspect_storage,
    preview_cleanup,
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
        if command[2:4] == ("venv", str(tmp_path / "envs" / task.id)):
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
        if command[2:4] == ("venv", str(tmp_path / "envs" / task.id)):
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
