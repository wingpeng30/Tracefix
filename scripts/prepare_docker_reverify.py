"""Stage one frozen task for Linux replay without copying a Windows checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from tracefix.real_benchmark import RealIssueTask
from tracefix.real_recipes import EnvironmentRecipe


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(source: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=source, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode:
        raise ValueError(f"git {arguments[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def stage(
    *, task_dir: Path, recipe_path: Path, source: Path, patch: Path,
    protocol: Path, output: Path,
) -> Path:
    task = RealIssueTask.load(task_dir)
    recipe = EnvironmentRecipe.model_validate_json(recipe_path.read_text(encoding="utf-8"))
    if recipe.task_id != task.id:
        raise ValueError("task and recipe identity differ")
    task.validate_checkout(source)
    if git(source, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("source checkout has uncommitted or untracked files")
    if (source / ".gitmodules").exists():
        raise ValueError("submodule source needs a separate archive contract")
    if not patch.is_file() or not patch.stat().st_size:
        raise ValueError("saved product patch is missing or empty")
    output.mkdir(parents=True, exist_ok=False)
    staged_task = output / "tasks" / task.id
    staged_task.parent.mkdir()
    shutil.copytree(task_dir, staged_task)
    (output / "recipes").mkdir()
    shutil.copy2(recipe_path, output / "recipes" / recipe_path.name)
    shutil.copy2(patch, output / "patch.diff")
    shutil.copy2(protocol, output / "protocol.json")
    bundle = output / "source.bundle"
    git(source, "bundle", "create", str(bundle.resolve()), "--all", "HEAD")
    git(source, "bundle", "verify", str(bundle.resolve()))
    files = {
        path.relative_to(output).as_posix(): digest(path)
        for path in sorted(output.rglob("*")) if path.is_file()
    }
    manifest = {
        "kind": "docker_reverification_input",
        "task_id": task.id,
        "source_commit": task.base_commit,
        "source_path_at_staging": str(source.resolve()),
        "recipe_fingerprint": recipe.fingerprint,
        "files": files,
    }
    destination = output / "input-manifest.json"
    destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("task-dir", "recipe-path", "source", "patch", "protocol", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    arguments = parser.parse_args()
    print(stage(**vars(arguments)))
