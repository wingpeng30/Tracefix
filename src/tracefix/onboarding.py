"""Read-only preflight and checked patch export for ordinary local runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from tracefix.messages import ToolCall
from tracefix.provenance import inspect_test_environment
from tracefix.report import _read_run
from tracefix.runtime import RunConfig, TraceFixRunner, load_environment_file
from tracefix.tools.builtin import ApplyPatchTool, RunTestsTool


def doctor(settings: dict[str, Any], *, prepare: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix})

    git = shutil.which("git")
    add("git", git is not None, git or "Git 未安装", "安装 Git 并加入 PATH")
    repo = settings["repo"]
    ordinary_docker = (
        settings.get("execution_backend") == "docker"
        and settings.get("docker_profile") == "ordinary"
    )
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
    if ordinary_docker:
        docker = shutil.which("docker")
        add("docker_cli", docker is not None, docker or "Docker CLI 未安装", "安装 Docker")
        image_id = settings.get("docker_image_id")
        valid_image = isinstance(image_id, str) and re.fullmatch(
            r"sha256:[0-9a-f]{64}", image_id
        ) is not None
        if docker and valid_image:
            try:
                inspected = subprocess.run(
                    [docker, "image", "inspect", "--format", "{{.Id}}", image_id],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                valid_image = inspected.returncode == 0 and inspected.stdout.strip() == image_id
            except (OSError, subprocess.SubprocessError):
                valid_image = False
        add("docker_image", valid_image, str(image_id or "未配置"),
            "运行 tracefix docker-prepare 并配置返回的完整 image_id")
        if settings.get("skills_root") is not None:
            add("docker_skills", False, "普通 Docker 首版不支持宿主自定义 Skills 目录")
    else:
        add(
            "test_python", python.is_file(), str(python),
            "指定含 pytest 与项目依赖的 --test-python",
        )
    if not ordinary_docker and python.is_file():
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
    elif not ordinary_docker:
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
    skills_root = settings.get("skills_root")
    if skills_root is not None:
        approved = Path(skills_root).expanduser().resolve()
        outside_repo = not (repo and approved.is_relative_to(Path(repo).expanduser().resolve()))
        add(
            "skills_directory",
            approved.is_dir() and outside_repo,
            str(approved),
            "指定已审核且位于目标仓库之外的 Skills 目录",
        )
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
    if prepare:
        if all(item["ok"] for item in checks):
            try:
                # A temporary clone exercises the same checkout and source-import
                # preparation as run, without constructing a model client.
                with tempfile.TemporaryDirectory(prefix="tracefix-doctor-") as temporary:
                    root = Path(temporary)
                    if ordinary_docker:
                        from tracefix.docker_backend import DockerToolBackend

                        backend = DockerToolBackend(
                            task_id="tracefix-ordinary", input_root=root,
                            run_dir=root, run_id=f"doctor-{uuid4().hex}",
                            profile="ordinary", image_id=settings["docker_image_id"],
                        )
                        try:
                            backend.prepare(commit, Path(repo),
                                            Path(__file__).resolve().parents[2],
                                            source_import_probe=module)
                            result = backend.workspace_preparation
                        finally:
                            backend.close(remove=True)
                    else:
                        workspace = TraceFixRunner._clone_repository(Path(repo), root / "workspace")
                        config = RunConfig(
                            repo=Path(repo), task="TraceFix environment preflight",
                            test_python_executable=python, test_target=target,
                            source_import=module,
                        )
                        result = TraceFixRunner._prepare_workspace(config, workspace, root)
                    probe = result.get("source_import_probe")
                    add("prepared_checkout", result.get("success") is True,
                        json.dumps(probe or result, ensure_ascii=False),
                        "检查任务镜像、pytest 和源码导入位置")
            except Exception as exc:
                add("prepared_checkout", False, str(exc), "检查仓库、解释器和项目依赖")
        else:
            add("prepared_checkout", False, "基础预检未通过，未创建临时 checkout")
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


def verify_patch(run: Path) -> dict[str, Any]:
    """Reapply a saved patch in a fresh local checkout and rerun its public test."""
    run_dir = _read_run(run)
    manifest_path = run_dir / "session.json"
    if not manifest_path.is_file():
        raise ValueError("运行缺少可核验的 session.json 配置")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config = RunConfig.model_validate(manifest["config"])
    if hashlib.sha256(config.model_dump_json().encode("utf-8")).hexdigest() != (
        manifest["identity"].get("config_sha256")
    ):
        raise ValueError("运行配置身份与 session.json 不符")
    if config.execution_backend == "docker" and config.docker_profile == "ordinary":
        from tracefix.ordinary_docker import verify_docker_patch

        result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        patch = (run_dir / "patch.diff").read_bytes()
        return verify_docker_patch(run_dir, config, manifest, result, patch)
    if config.execution_backend != "local" or not config.test_target or not config.source_import:
        raise ValueError("独立验证仅支持记录了测试目标与源码导入的普通本地运行")
    environment_sha = inspect_test_environment(
        config.test_python_executable or sys.executable,
        pythonpath_entries=config.test_pythonpath_entries,
    ).fingerprint_sha256
    if environment_sha != manifest["identity"].get("test_environment_sha256"):
        raise ValueError("测试解释器及依赖身份与原运行不符")
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    patch = (run_dir / "patch.diff").read_bytes()
    patch_sha = hashlib.sha256(patch).hexdigest()
    if not patch or patch_sha != result.get("patch_sha256"):
        raise ValueError("补丁为空或与 result.json 身份不符")
    source, commit = TraceFixRunner._validate_source_repository(config.repo)
    if commit != result.get("source_commit") or commit != manifest["identity"]["source_commit"]:
        raise ValueError("源仓库提交与运行记录不符")
    validation_path = run_dir / "independent-validation.json"
    if validation_path.exists():
        raise ValueError("独立验证结果已存在；请保留原始证据")
    evidence_dir = run_dir / "independent-validation-evidence"
    with tempfile.TemporaryDirectory(prefix="tracefix-verify-") as temporary:
        temporary_root = Path(temporary)
        workspace = TraceFixRunner._clone_repository(source, temporary_root / "workspace")
        preparation = TraceFixRunner._prepare_workspace(config, workspace, temporary_root)
        if preparation.get("success") is not True:
            raise ValueError(f"独立验证环境预检失败: {preparation.get('failure')}")
        patch_result = ApplyPatchTool(workspace).execute(ToolCall(
            id="independent-patch", name="apply_patch",
            arguments={"patch": patch.decode("utf-8")},
        ))
        if not patch_result.success:
            raise ValueError(f"补丁无法应用于原始提交: {patch_result.error}")
        test_result = RunTestsTool(
            workspace,
            python_executable=config.test_python_executable,
            pythonpath_entries=config.test_pythonpath_entries,
            evidence_dir=evidence_dir,
        ).execute(ToolCall(
            id="independent-test", name="run_tests",
            arguments={"command": f"pytest -q {config.test_target}"},
        ))
        record = {
            "schema_version": 1,
            "source_commit": commit,
            "patch_sha256": patch_sha,
            "test_target": config.test_target,
            "test_python": str(config.test_python_executable or Path(sys.executable)),
            "test_environment_sha256": environment_sha,
            "preparation": preparation,
            "test": test_result.model_dump(mode="json"),
            "passed": test_result.success and test_result.output.get("test_status") == "passed",
        }
    validation_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record
