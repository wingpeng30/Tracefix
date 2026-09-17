"""真实任务独立 Python 环境的准备、复用与溯源。"""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from tracefix.exceptions import BenchmarkError
from tracefix.provenance import TestEnvironmentProvenance, inspect_test_environment
from tracefix.real_benchmark import RealIssueTask, load_real_issue_tasks
from tracefix.real_recipes import EnvironmentRecipe, load_environment_recipes

TUNA_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
_OWNER_MARKER = ".tracefix-owner.json"
_ENVIRONMENT_MARKER = ".tracefix-environment.json"


class InterpreterInfo(BaseModel):
    """本机可用 Python 解释器的不可变盘点记录。"""

    model_config = ConfigDict(extra="forbid")

    executable: str
    version: str
    source: str


class StorageReport(BaseModel):
    """TraceFix 管理目录和所在磁盘的空间报告。"""

    model_config = ConfigDict(extra="forbid")

    root: str
    free_bytes: int = Field(ge=0)
    managed_environment_bytes: int = Field(ge=0)
    managed_workspace_bytes: int = Field(ge=0)
    interpreter_count: int = Field(ge=0)


class CleanupPreview(BaseModel):
    """只枚举带 TraceFix 标记的可回收目录，避免误删用户目录。"""

    model_config = ConfigDict(extra="forbid")

    paths: tuple[str, ...]
    bytes_reclaimable: int = Field(ge=0)


class EnvironmentPreparationResult(BaseModel):
    """一道任务环境准备的可审计结果，不把安装失败伪装成测试失败。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: Literal[
        "ready",
        "reused",
        "incompatible",
        "install_failed",
        "interpreter_missing",
        "platform_unsupported",
        "space_insufficient",
    ]
    python_executable: str | None = None
    python_version: str | None = None
    environment_fingerprint: str | None = None
    source_commit: str
    install_commands: tuple[tuple[str, ...], ...] = ()
    logs: tuple[str, ...] = ()
    environment: TestEnvironmentProvenance | None = None
    recipe_hash: str | None = None


class EnvironmentPreparationSummary(BaseModel):
    """批量准备独立测试环境的机器可读汇总。"""

    model_config = ConfigDict(extra="forbid")

    created_at: datetime
    source_root: str
    environment_root: str
    python_executable: str | None = None
    index_url: str
    results: tuple[EnvironmentPreparationResult, ...]
    summary_path: str


class EnvironmentPreparationConfig(BaseModel):
    """环境准备命令的输入；解释器由调用方显式选择，避免下载系统级 Python。"""

    model_config = ConfigDict(extra="forbid")

    tasks_dir: Path
    source_root: Path
    environment_root: Path
    output_dir: Path
    task_ids: tuple[str, ...] = ()
    python_executable: Path | None = None
    index_url: str = TUNA_INDEX_URL
    timeout_seconds: int = Field(default=600, ge=30, le=1800)
    recipes_dir: Path = Path("benchmarks/real_recipes")
    min_free_gib: int = Field(default=10, ge=1, le=500)
    allow_create_interpreter: bool = True


class RealEnvironmentPreparer:
    """为固定源码任务创建或复用独立虚拟环境。"""

    def prepare(self, config: EnvironmentPreparationConfig) -> EnvironmentPreparationSummary:
        """逐题准备环境；单题失败仍会留下结构化结果并继续后续任务。"""
        tasks = load_real_issue_tasks(config.tasks_dir, task_ids=config.task_ids)
        recipes = load_environment_recipes(config.recipes_dir)
        root = config.environment_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        output = config.output_dir.expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        interpreters = discover_interpreters(root, explicit=(config.python_executable,))
        report = inspect_storage(root)
        results = tuple(
            self._prepare_one(
                task, recipes.get(task.id), config, root, interpreters, report
            )
            for task in tasks
        )
        path = output / "environment-preparation.json"
        summary = EnvironmentPreparationSummary(
            created_at=datetime.now(UTC),
            source_root=str(config.source_root.expanduser().resolve()),
            environment_root=str(root),
            python_executable=str(config.python_executable.expanduser().resolve())
            if config.python_executable
            else None,
            index_url=config.index_url,
            results=results,
            summary_path=str(path),
        )
        path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
        return summary

    def _prepare_one(
        self,
        task: RealIssueTask,
        recipe: EnvironmentRecipe | None,
        config: EnvironmentPreparationConfig,
        root: Path,
        interpreters: tuple[InterpreterInfo, ...],
        storage: StorageReport,
    ) -> EnvironmentPreparationResult:
        """创建单题环境，并只安装该固定源码声明的依赖。"""
        source = (config.source_root / task.id).expanduser().resolve()
        if recipe is not None and not recipe.supports_current_platform():
            return EnvironmentPreparationResult(
                task_id=task.id,
                status="platform_unsupported",
                source_commit=task.base_commit,
                recipe_hash=recipe.fingerprint,
                logs=("task recipe does not support the current platform",),
            )
        if storage.free_bytes < config.min_free_gib * 1024**3:
            return EnvironmentPreparationResult(
                task_id=task.id,
                status="space_insufficient",
                source_commit=task.base_commit,
                recipe_hash=recipe.fingerprint if recipe else None,
                logs=(f"free space is below {config.min_free_gib} GiB",),
            )
        interpreter_info = _select_interpreter(recipe, config.python_executable, interpreters)
        if (
            interpreter_info is None
            and config.allow_create_interpreter
            and recipe
            and recipe.python_versions
        ):
            interpreter_info = _create_managed_interpreter(
                root=root, version=recipe.python_versions[0]
            )
        if interpreter_info is None:
            return EnvironmentPreparationResult(
                task_id=task.id,
                status="interpreter_missing",
                source_commit=task.base_commit,
                recipe_hash=recipe.fingerprint if recipe else None,
                logs=("no compatible local Python interpreter was found",),
            )
        interpreter = Path(interpreter_info.executable)
        environment = _select_matching_managed_environment(
            root, task, config.index_url, recipe, interpreter_info
        )
        if not source.is_dir():
            return EnvironmentPreparationResult(
                task_id=task.id,
                status="install_failed",
                source_commit=task.base_commit,
                logs=(f"fixed source checkout does not exist: {source}",),
            )
        python = _environment_python(environment)
        marker = _marker_path(environment)
        if python.is_file() and _marker_matches(
            marker, task, config.index_url, recipe, interpreter_info, python
        ):
            try:
                provenance = inspect_test_environment(python)
            except ValueError:
                pass
            else:
                return EnvironmentPreparationResult(
                    task_id=task.id,
                    status="reused",
                    python_executable=str(python),
                    python_version=provenance.python_version,
                    environment_fingerprint=_fingerprint(task, provenance, recipe),
                    source_commit=task.base_commit,
                    environment=provenance,
                    recipe_hash=recipe.fingerprint if recipe else None,
                )
        commands: list[tuple[str, ...]] = []
        logs: list[str] = []
        # 健康标记缺失、损坏或依赖漂移时绝不删除旧现场。选择一个新的、已登记的
        # 目录重建，供人工保留和检查旧安装日志。
        if environment.exists():
            environment = _new_managed_environment(root, task.id)
            python = _environment_python(environment)
            marker = _marker_path(environment)
        _register_environment(root, environment, task.id)
        process_environment = _managed_process_environment(environment)
        if not python.is_file():
            create = (str(interpreter), "-m", "venv", str(environment))
            commands.append(create)
            created = _run(create, source.parent, config.timeout_seconds, process_environment)
            logs.append(_log("create", created))
            if created.returncode != 0:
                return _failed(task, "install_failed", commands, logs, recipe)
        python = _environment_python(environment)
        # 即使是 pip 自身的升级也统一走清华镜像，避免某一步意外访问默认 PyPI。
        bootstrap = (
            str(python),
            "-m",
            "pip",
            "install",
            "-i",
            config.index_url,
            "--upgrade",
            "pip",
        )
        commands.append(bootstrap)
        boot = _run(bootstrap, source, config.timeout_seconds, process_environment)
        logs.append(_log("pip_bootstrap", boot))
        if boot.returncode != 0:
            return _failed(task, "install_failed", commands, logs, recipe)
        # 依赖元数据安装必须不写固定源码。先复制到环境自己的临时构建目录，完成后
        # 回收副本；base/gold 测试依然通过 PYTHONPATH 从各自 checkout 导入代码。
        if recipe and recipe.extra_dependencies:
            extras = (
                str(python),
                "-m",
                "pip",
                "install",
                "-i",
                config.index_url,
                *recipe.extra_dependencies,
            )
            commands.append(extras)
            installed_extras = _run(extras, source, config.timeout_seconds, process_environment)
            logs.append(_log("recipe_dependencies", installed_extras))
            if installed_extras.returncode != 0:
                return _failed(task, "install_failed", commands, logs, recipe)
        build_source = environment / ".tracefix-build-source"
        shutil.copytree(source, build_source, ignore=shutil.ignore_patterns(".git", ".tracefix*"))
        install = (str(python), "-m", "pip", "install", "-i", config.index_url, ".")
        commands.append(install)
        installed = _run(install, build_source, config.timeout_seconds, process_environment)
        logs.append(_log("project_install", installed))
        _remove_managed_child(environment, build_source)
        if installed.returncode != 0:
            status = (
                "incompatible"
                if _looks_incompatible(installed.stderr + installed.stdout)
                else "install_failed"
            )
            return _failed(task, status, commands, logs, recipe)
        pytest = (str(python), "-m", "pip", "install", "-i", config.index_url, "pytest")
        commands.append(pytest)
        installed_pytest = _run(pytest, source, config.timeout_seconds, process_environment)
        logs.append(_log("pytest_install", installed_pytest))
        if installed_pytest.returncode != 0:
            return _failed(task, "install_failed", commands, logs, recipe)
        try:
            provenance = inspect_test_environment(python)
        except ValueError as exc:
            logs.append(str(exc))
            return _failed(task, "install_failed", commands, logs, recipe)
        marker.write_text(
            json.dumps(
                {
                    "task_id": task.id,
                    "base_commit": task.base_commit,
                    "index_url": config.index_url,
                    "recipe_hash": recipe.fingerprint if recipe else None,
                    "interpreter_version": interpreter_info.version,
                    "environment_fingerprint": _fingerprint(task, provenance, recipe),
                    "dependency_fingerprint": provenance.fingerprint_sha256,
                    "managed_kind": "environment",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return EnvironmentPreparationResult(
            task_id=task.id,
            status="ready",
            python_executable=str(python),
            python_version=provenance.python_version,
            environment_fingerprint=_fingerprint(task, provenance, recipe),
            source_commit=task.base_commit,
            install_commands=tuple(commands),
            logs=tuple(logs),
            environment=provenance,
            recipe_hash=recipe.fingerprint if recipe else None,
        )


def _environment_python(environment: Path) -> Path:
    """兼容 Windows/POSIX venv 布局。"""
    return environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _marker_path(environment: Path) -> Path:
    """返回只在完整安装成功后写入的环境健康标记。"""
    return environment / _ENVIRONMENT_MARKER


def _is_safe_managed_path(root: Path, path: Path) -> bool:
    """仅接受管理根目录内、没有链接跳转的直接管理子目录。"""
    # 在 resolve 前拒绝 ``..``，否则 ``root/../outside`` 会在语义上逃逸后又被
    # Path.relative_to 的纯词法比较错误接受。
    if ".." in path.parts or _is_link(path):
        return False
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError:
        return False
    if not relative.parts or path.absolute() == root.absolute():
        return False
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_link(current):
            return False
    return True


def _is_link(path: Path) -> bool:
    """同时识别 POSIX 符号链接和 Windows junction。"""
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _owner_matches(root: Path, environment: Path, task_id: str) -> bool:
    """确认目录是本根目录为该任务登记的环境，而不是碰巧同名的用户目录。"""
    if not _is_safe_managed_path(root, environment):
        return False
    try:
        payload = json.loads((environment / _OWNER_MARKER).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload == {
        "managed_kind": "environment",
        "task_id": task_id,
        "environment_root": str(root),
        "environment_path": str(environment.resolve()),
    }


def _select_managed_environment(root: Path, task_id: str) -> Path:
    """复用已登记环境；旧的未登记目录保持原状并绕开。"""
    base = root / task_id
    candidates = (base, *sorted(root.glob(f"{task_id}--rebuild-*")))
    for candidate in candidates:
        if _owner_matches(root, candidate, task_id):
            return candidate
    return base if not base.exists() else _new_managed_environment(root, task_id, create=False)


def _select_matching_managed_environment(
    root: Path,
    task: RealIssueTask,
    index_url: str,
    recipe: EnvironmentRecipe | None,
    interpreter: InterpreterInfo,
) -> Path:
    """优先选取实际依赖仍匹配本任务配方的最新已登记环境。"""
    candidates = (root / task.id, *sorted(root.glob(f"{task.id}--rebuild-*")))
    for candidate in reversed(candidates):
        python = _environment_python(candidate)
        if (
            _owner_matches(root, candidate, task.id)
            and python.is_file()
            and _marker_matches(_marker_path(candidate), task, index_url, recipe, interpreter, python)
        ):
            return candidate
    return _select_managed_environment(root, task.id)


def resolve_managed_environment_python(root: Path, task_id: str) -> Path:
    """定位已登记、依赖指纹仍健康的任务解释器。"""
    managed_root = root.expanduser().resolve()
    candidates = (managed_root / task_id, *sorted(managed_root.glob(f"{task_id}--rebuild-*")))
    for environment in reversed(candidates):
        if not _owner_matches(managed_root, environment, task_id):
            continue
        for python in (
            environment / "Scripts" / "python.exe",
            environment / "bin" / "python",
        ):
            if python.is_file() and _registered_environment_is_healthy(environment, task_id, python):
                return python
    raise BenchmarkError(
        "real task test interpreter does not exist in a registered environment",
        context={"task_id": task_id, "environment_root": str(managed_root)},
    )


def _registered_environment_is_healthy(environment: Path, task_id: str, python: Path) -> bool:
    """运行入口只复用具备完整健康标记且依赖未漂移的环境。"""
    try:
        payload = json.loads(_marker_path(environment).read_text(encoding="utf-8"))
        current = inspect_test_environment(python)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        payload.get("task_id") == task_id
        and payload.get("managed_kind") == "environment"
        and payload.get("dependency_fingerprint") == current.fingerprint_sha256
    )


def _new_managed_environment(root: Path, task_id: str, *, create: bool = True) -> Path:
    """分配未使用的重建目录；从不覆盖或回收旧目录。"""
    candidate = root / f"{task_id}--rebuild-{uuid4().hex[:12]}"
    if not _is_safe_managed_path(root, candidate):
        raise BenchmarkError("refusing unsafe managed environment path", context={"path": str(candidate)})
    if create:
        candidate.mkdir(parents=False, exist_ok=False)
    return candidate


def _register_environment(root: Path, environment: Path, task_id: str) -> None:
    """在创建任何 venv 内容前写入任务所有权标记。"""
    if not _is_safe_managed_path(root, environment):
        raise BenchmarkError("refusing to register an unmanaged path", context={"path": str(environment)})
    environment.mkdir(parents=False, exist_ok=True)
    marker = environment / _OWNER_MARKER
    payload = {
        "managed_kind": "environment",
        "task_id": task_id,
        "environment_root": str(root),
        "environment_path": str(environment.resolve()),
    }
    if marker.exists() and not _owner_matches(root, environment, task_id):
        raise BenchmarkError("environment ownership does not match task", context={"path": str(environment)})
    marker.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _remove_managed_child(environment: Path, child: Path) -> None:
    """只回收本次环境中的构建副本，拒绝链接和边界外路径。"""
    if not child.exists():
        return
    if child.parent != environment or child.is_symlink():
        raise BenchmarkError("refusing to remove an unmanaged build path", context={"path": str(child)})
    shutil.rmtree(child)


def _marker_matches(
    marker: Path,
    task: RealIssueTask,
    index_url: str,
    recipe: EnvironmentRecipe | None,
    interpreter: InterpreterInfo,
    python: Path,
) -> bool:
    """仅复用同一提交、配方、解释器和镜像已完成的健康环境。"""
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    identity_matches = (
        payload.get("task_id") == task.id
        and payload.get("base_commit") == task.base_commit
        and payload.get("index_url") == index_url
        and payload.get("recipe_hash") == (recipe.fingerprint if recipe else None)
        and payload.get("interpreter_version") == interpreter.version
    )
    if not identity_matches:
        return False
    try:
        current = inspect_test_environment(python)
    except ValueError:
        return False
    # pip 或手工安装改变依赖后必须重新准备，不能继续把旧 marker 当作健康环境。
    return payload.get("dependency_fingerprint") == current.fingerprint_sha256


def _managed_process_environment(environment: Path) -> dict[str, str]:
    """让 venv、ensurepip 与 pip 都使用可控的项目临时目录。"""
    temporary = environment.parent / ".tracefix-environment-tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    values = dict(os.environ)
    values.update({"TEMP": str(temporary), "TMP": str(temporary), "PIP_NO_CACHE_DIR": "1"})
    return values


def _run(
    command: tuple[str, ...],
    cwd: Path,
    timeout: int,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """执行没有 Shell 的依赖安装命令。"""
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(command, 1, "", str(exc))


def _log(label: str, result: subprocess.CompletedProcess[str]) -> str:
    """保存有限安装诊断，避免将完整依赖输出写进 Git 追踪结果。"""
    message = (result.stderr or result.stdout).strip().replace("\r\n", "\n")
    return f"{label}: returncode={result.returncode}; {message[-2000:]}"


def _looks_incompatible(message: str) -> bool:
    """区分 Python 版本限制与普通网络/依赖安装故障。"""
    lowered = message.casefold()
    return "requires-python" in lowered or "requires a different python" in lowered


def _failed(
    task: RealIssueTask,
    status: Literal["incompatible", "install_failed"],
    commands: list[tuple[str, ...]],
    logs: list[str],
    recipe: EnvironmentRecipe | None,
) -> EnvironmentPreparationResult:
    """统一失败结果并保留已执行安装步骤。"""
    return EnvironmentPreparationResult(
        task_id=task.id,
        status=status,
        source_commit=task.base_commit,
        install_commands=tuple(commands),
        logs=tuple(logs),
        recipe_hash=recipe.fingerprint if recipe else None,
    )


def _fingerprint(
    task: RealIssueTask,
    environment: TestEnvironmentProvenance,
    recipe: EnvironmentRecipe | None,
) -> str:
    """将固定提交和依赖指纹共同纳入环境身份。"""
    payload = json.dumps(
        {
            "task_id": task.id,
            "commit": task.base_commit,
            "environment": environment.fingerprint_sha256,
            "recipe": recipe.fingerprint if recipe else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def discover_interpreters(
    environment_root: Path,
    *,
    explicit: Iterable[Path | None] = (),
) -> tuple[InterpreterInfo, ...]:
    """盘点 Launcher、Conda、已管理环境和显式路径中的可执行 Python。"""
    candidates: dict[Path, str] = {Path(sys.executable).resolve(): "tracefix_runtime"}
    for value in explicit:
        if value is not None:
            candidates[value.expanduser().resolve()] = "explicit"
    launcher = _run(("py", "-0p"), Path.cwd(), 20)
    if launcher.returncode == 0:
        for line in launcher.stdout.splitlines():
            match = re.search(r"([A-Za-z]:\\.+python(?:\.exe)?)$", line.strip(), re.IGNORECASE)
            if match:
                candidates[Path(match.group(1)).resolve()] = "python_launcher"
    conda_prefix = os.getenv("CONDA_PREFIX")
    if conda_prefix:
        candidates[(Path(conda_prefix) / "python.exe").resolve()] = "conda_active"
    conda = Path("D:/anaconda3/Scripts/conda.exe")
    if conda.is_file():
        listed = _run((str(conda), "env", "list", "--json"), Path.cwd(), 30)
        if listed.returncode == 0:
            try:
                prefixes = json.loads(listed.stdout).get("envs", [])
            except json.JSONDecodeError:
                prefixes = []
            for prefix in prefixes:
                if isinstance(prefix, str):
                    candidates[(Path(prefix) / "python.exe").resolve()] = "conda_environment"
    # 某些受限 Windows 会阻止 conda 查询 CUDA 虚拟包；此时只扫描 Conda 的标准 envs
    # 目录，不执行或改写任何用户环境。
    for base in (Path("D:/anaconda3"), Path(conda_prefix) if conda_prefix else None):
        if base is None:
            continue
        envs = base / "envs"
        if envs.is_dir():
            for prefix in envs.iterdir():
                candidates[(prefix / "python.exe").resolve()] = "conda_environment"
    # 已注册的 TraceFix venv 和解释器都在本根目录内；扫描深度保持有限。
    for relative in (".tracefix-interpreters",):
        folder = environment_root / relative
        if folder.is_dir():
            for executable in folder.glob("*/python.exe"):
                candidates[executable.resolve()] = "tracefix_managed"
    records: list[InterpreterInfo] = []
    for executable, source in candidates.items():
        if executable == Path(sys.executable).resolve():
            records.append(
                InterpreterInfo(
                    executable=str(executable),
                    version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                    source=source,
                )
            )
            continue
        result = _run((str(executable), "--version"), Path.cwd(), 20)
        version = (result.stdout or result.stderr).strip().removeprefix("Python ")
        if result.returncode == 0 and re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
            records.append(
                InterpreterInfo(executable=str(executable), version=version, source=source)
            )
    return tuple(sorted(records, key=lambda item: (_version_key(item.version), item.executable), reverse=True))


def inspect_storage(environment_root: Path) -> StorageReport:
    """统计 TraceFix 自己管理的环境与工作区，而不扫描用户的其他项目。"""
    root = environment_root.expanduser().resolve()
    usage = shutil.disk_usage(root.anchor or root)
    environments = workspaces = 0
    if root.is_dir():
        for marker in root.rglob(".tracefix-environment.json"):
            if _owner_matches(root, marker.parent, _owner_task_id(marker.parent)):
                environments += _directory_size(marker.parent)
        for marker in root.rglob(".tracefix-workspace.json"):
            if _is_safe_managed_path(root, marker.parent) and not marker.parent.is_symlink():
                workspaces += _directory_size(marker.parent)
    return StorageReport(
        root=str(root),
        free_bytes=usage.free,
        managed_environment_bytes=environments,
        managed_workspace_bytes=workspaces,
        interpreter_count=len(discover_interpreters(root)),
    )


def preview_cleanup(environment_root: Path) -> CleanupPreview:
    """返回可回收的 TraceFix 标记目录；调用方决定是否显式删除。"""
    root = environment_root.expanduser().resolve()
    paths: list[Path] = []
    if root.is_dir():
        for name in (".tracefix-environment.json", ".tracefix-workspace.json"):
            for marker in root.rglob(name):
                path = marker.parent
                if name == _ENVIRONMENT_MARKER:
                    if _owner_matches(root, path, _owner_task_id(path)):
                        paths.append(path)
                elif _is_safe_managed_path(root, path) and not path.is_symlink():
                    paths.append(path)
    unique = tuple(sorted({path.resolve() for path in paths}))
    return CleanupPreview(
        paths=tuple(str(path) for path in unique),
        bytes_reclaimable=sum(_directory_size(path) for path in unique),
    )


def apply_cleanup(environment_root: Path) -> CleanupPreview:
    """删除预览出的已登记目录；仅接受环境根目录内的 TraceFix 标记。"""
    preview = preview_cleanup(environment_root)
    root = environment_root.expanduser().resolve()
    for raw in preview.paths:
        path = Path(raw)
        if (
            not _is_safe_managed_path(root, path)
            or not (path / ".tracefix-environment.json").is_file()
            and not (path / ".tracefix-workspace.json").is_file()
            or (path / ".tracefix-environment.json").is_file()
            and not _owner_matches(root, path, _owner_task_id(path))
        ):
            raise BenchmarkError("refusing to clean an unmanaged path", context={"path": str(path)})
        shutil.rmtree(path)
    return preview


def _owner_task_id(environment: Path) -> str:
    """读取所有权中的任务 ID；无效值会使后续所有权检查失败。"""
    try:
        value = json.loads((environment / _OWNER_MARKER).read_text(encoding="utf-8")).get("task_id")
    except (OSError, json.JSONDecodeError):
        return ""
    return value if isinstance(value, str) else ""


def _select_interpreter(
    recipe: EnvironmentRecipe | None,
    explicit: Path | None,
    interpreters: tuple[InterpreterInfo, ...],
) -> InterpreterInfo | None:
    """优先校验显式解释器，否则按配方从本机盘点结果中选择。"""
    if explicit is not None:
        found = next(
            (
                item
                for item in interpreters
                if Path(item.executable) == explicit.expanduser().resolve()
            ),
            None,
        )
        return (
            found if found and (recipe is None or recipe.supports_python(found.version)) else None
        )
    return next(
        (item for item in interpreters if recipe is None or recipe.supports_python(item.version)),
        None,
    )


def _create_managed_interpreter(root: Path, version: str) -> InterpreterInfo | None:
    """仅在缺少配方要求版本时创建一个 TraceFix 专用 Conda 解释器。"""
    conda_candidates = (
        Path("D:/anaconda3/Scripts/conda.exe"),
        Path("D:/anaconda3/condabin/conda.bat"),
    )
    conda = next((path for path in conda_candidates if path.is_file()), None)
    if conda is None:
        return None
    target = root / ".tracefix-interpreters" / f"python-{version}"
    executable = target / "python.exe"
    if not executable.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        result = _run(
            (str(conda), "create", "--prefix", str(target), f"python={version}", "--yes"),
            root,
            1800,
        )
        if result.returncode != 0:
            return None
        (target / ".tracefix-managed-interpreter.json").write_text(
            json.dumps({"version": version, "managed_kind": "interpreter"}), encoding="utf-8"
        )
    found = discover_interpreters(root)
    return next((item for item in found if Path(item.executable) == executable.resolve()), None)


def _directory_size(path: Path) -> int:
    """统计单个已知管理目录大小；无法读取的文件不影响其他统计。"""
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                total += child.stat().st_size
    except OSError:
        return total
    return total


def _version_key(value: str) -> tuple[int, ...]:
    """将 Python 版本转为数值排序键，避免把 3.9 排在 3.12 前。"""
    return tuple(int(part) for part in value.split("."))
