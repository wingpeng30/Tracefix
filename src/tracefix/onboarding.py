"""Read-only preflight and checked patch export for ordinary local runs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from tracefix.report import _read_run
from tracefix.runtime import TraceFixRunner, load_environment_file


def doctor(settings: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix})

    git = shutil.which("git")
    add("git", git is not None, git or "Git 未安装", "安装 Git 并加入 PATH")
    repo = settings["repo"]
    if repo is None:
        add("repository", False, "缺少仓库路径", "设置 [run].repo 或 --repo")
    elif git:
        try:
            root, commit = TraceFixRunner._validate_source_repository(repo)
            add("repository", True, f"{root} @ {commit}")
        except Exception as exc:
            add("repository", False, str(exc), "提交或清理 Git 仓库后重试")
    python = settings["test_python_executable"] or Path(sys.executable)
    python = Path(python).expanduser().resolve()
    add("test_python", python.is_file(), str(python), "指定含 pytest 与项目依赖的 --test-python")
    if python.is_file():
        try:
            completed = subprocess.run(
                [str(python), "-c", "import pytest; print(pytest.__version__)"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            add(
                "pytest",
                completed.returncode == 0,
                completed.stdout.strip() or completed.stderr.strip()[-300:],
                "在测试解释器中安装 pytest",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            add("pytest", False, str(exc), "检查测试解释器")
    else:
        add("pytest", False, "测试解释器不存在")
    target = settings["test_target"]
    if target is not None:
        candidate = str(target).split("::", 1)[0]
        safe = (
            bool(candidate)
            and not Path(candidate).is_absolute()
            and ".." not in Path(candidate).parts
        )
        exists = bool(repo and safe and (Path(repo) / candidate).is_file())
        add("test_target", exists, str(target), "指定仓库内已有的相对 pytest 文件")
    module = settings["source_import"]
    valid_module = isinstance(module, str) and all(
        part.isidentifier() for part in module.split(".")
    )
    add("source_import", valid_module, str(module or "未配置"), "指定待测包的导入名，例如 catalog")
    output = settings["output_dir"] or Path(os.getenv("TRACEFIX_RUNS_ROOT") or "runs")
    output = Path(output).expanduser().resolve()
    if repo is not None and output.is_relative_to(Path(repo).expanduser().resolve()):
        add("output", False, "输出目录位于源仓库内", "选择源仓库外的 --output-dir")
    else:
        parent = next((path for path in (output, *output.parents) if path.exists()), None)
        writable = bool(parent and parent.is_dir() and os.access(parent, os.W_OK))
        add("output", writable, str(output), "选择可写的 --output-dir")
    model = str(settings["model_name"])
    try:
        import litellm  # noqa: F401

        has_model = True
    except ImportError:
        has_model = False
    add("model_dependency", has_model, model, "安装 tracefix-agent[llm]")
    if model.casefold().startswith("deepseek/"):
        try:
            load_environment_file(settings["env_file"])
            present = bool(os.getenv("DEEPSEEK_API_KEY"))
            add(
                "credential",
                present,
                "已配置" if present else "缺少 DEEPSEEK_API_KEY",
                "设置环境变量或在 --env-file 中配置",
            )
        except Exception as exc:
            add("credential", False, str(exc), "检查 --env-file 和 llm 依赖")
    return {"ok": all(item["ok"] for item in checks), "checks": checks}


def export_patch(run: Path, output: Path) -> Path:
    run_dir = _read_run(run)
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    if not isinstance(result, dict) or not result.get("source_commit"):
        raise ValueError("运行缺少可核验的源码提交身份")
    patch = run_dir / "patch.diff"
    if not patch.is_file():
        raise ValueError("运行缺少 patch.diff")
    data = patch.read_bytes()
    if not data or not result.get("changed_files"):
        raise ValueError("运行没有可导出的补丁")
    expected_hash = result.get("patch_sha256")
    if expected_hash is not None and hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError("补丁内容与运行结果记录的 SHA-256 不匹配")
    recorded_name = str(result.get("diff_path", "")).replace("\\", "/").split("/")[-1]
    if recorded_name != "patch.diff":
        raise ValueError("结果中的补丁路径不匹配")
    destination = output.expanduser().resolve()
    checksum = destination.parent / f"{destination.name}.sha256"
    if destination.exists() or checksum.exists() or destination == patch.resolve():
        raise ValueError("导出目标必须是尚不存在的新文件")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    checksum.write_text(f"{digest}  {destination.name}\n", encoding="ascii")
    return destination
