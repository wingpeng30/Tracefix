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

from pydantic import BaseModel, ConfigDict, Field

from tracefix.exceptions import BenchmarkError
from tracefix.provenance import TestEnvironmentProvenance, inspect_test_environment
from tracefix.real_benchmark import RealIssueTask, load_real_issue_tasks
from tracefix.real_recipes import EnvironmentRecipe, load_environment_recipes

TUNA_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"


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
                task, recipes.get(task.id), config, root / task.id, interpreters, report
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
        environment: Path,
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
                root=environment.parent, version=recipe.python_versions[0]
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
            marker, task, config.index_url, recipe, interpreter_info
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
        if not python.is_file():
            create = (str(interpreter), "-m", "venv", str(environment))
            commands.append(create)
            created = _run(create, source.parent, config.timeout_seconds)
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
        boot = _run(bootstrap, source, config.timeout_seconds)
        logs.append(_log("pip_bootstrap", boot))
        if boot.returncode != 0:
            return _failed(task, "install_failed", commands, logs, recipe)
        # 安装固定源码所声明的依赖和项目元数据，但不使用 editable 安装；
        # RunTestsTool 会以工作区和 src/ 为优先导入路径，因此 base/gold 两个
        # 副本均从各自 clone 加载实现，而不是从环境中的固定提交加载。
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
            installed_extras = _run(extras, source, config.timeout_seconds)
            logs.append(_log("recipe_dependencies", installed_extras))
            if installed_extras.returncode != 0:
                return _failed(task, "install_failed", commands, logs, recipe)
        install = (str(python), "-m", "pip", "install", "-i", config.index_url, ".")
        commands.append(install)
        installed = _run(install, source, config.timeout_seconds)
        logs.append(_log("project_install", installed))
        if installed.returncode != 0:
            status = (
                "incompatible"
                if _looks_incompatible(installed.stderr + installed.stdout)
                else "install_failed"
            )
            return _failed(task, status, commands, logs, recipe)
        pytest = (str(python), "-m", "pip", "install", "-i", config.index_url, "pytest")
        commands.append(pytest)
        installed_pytest = _run(pytest, source, config.timeout_seconds)
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
    return environment / ".tracefix-environment.json"


def _marker_matches(
    marker: Path,
    task: RealIssueTask,
    index_url: str,
    recipe: EnvironmentRecipe | None,
    interpreter: InterpreterInfo,
) -> bool:
    """仅复用同一提交、配方、解释器和镜像已完成的健康环境。"""
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        payload.get("task_id") == task.id
        and payload.get("base_commit") == task.base_commit
        and payload.get("index_url") == index_url
        and payload.get("recipe_hash") == (recipe.fingerprint if recipe else None)
        and payload.get("interpreter_version") == interpreter.version
    )


def _run(command: tuple[str, ...], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
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
            environments += _directory_size(marker.parent)
        for marker in root.rglob(".tracefix-workspace.json"):
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
            paths.extend(marker.parent for marker in root.rglob(name))
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
            not path.is_relative_to(root)
            or not (path / ".tracefix-environment.json").is_file()
            and not (path / ".tracefix-workspace.json").is_file()
        ):
            raise BenchmarkError("refusing to clean an unmanaged path", context={"path": str(path)})
        shutil.rmtree(path)
    return preview


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
