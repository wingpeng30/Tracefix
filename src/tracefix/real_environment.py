"""真实任务独立 Python 环境的准备、复用与溯源。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tracefix.provenance import TestEnvironmentProvenance, inspect_test_environment
from tracefix.real_benchmark import RealIssueTask, load_real_issue_tasks

TUNA_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"


class EnvironmentPreparationResult(BaseModel):
    """一道任务环境准备的可审计结果，不把安装失败伪装成测试失败。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: Literal["ready", "reused", "incompatible", "install_failed", "interpreter_missing"]
    python_executable: str | None = None
    python_version: str | None = None
    environment_fingerprint: str | None = None
    source_commit: str
    install_commands: tuple[tuple[str, ...], ...] = ()
    logs: tuple[str, ...] = ()
    environment: TestEnvironmentProvenance | None = None


class EnvironmentPreparationSummary(BaseModel):
    """批量准备独立测试环境的机器可读汇总。"""

    model_config = ConfigDict(extra="forbid")

    created_at: datetime
    source_root: str
    environment_root: str
    python_executable: str
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
    python_executable: Path = Field(default_factory=lambda: Path(sys.executable))
    index_url: str = TUNA_INDEX_URL
    timeout_seconds: int = Field(default=600, ge=30, le=1800)


class RealEnvironmentPreparer:
    """为固定源码任务创建或复用独立虚拟环境。"""

    def prepare(self, config: EnvironmentPreparationConfig) -> EnvironmentPreparationSummary:
        """逐题准备环境；单题失败仍会留下结构化结果并继续后续任务。"""
        tasks = load_real_issue_tasks(config.tasks_dir, task_ids=config.task_ids)
        root = config.environment_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        output = config.output_dir.expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        results = tuple(self._prepare_one(task, config, root / task.id) for task in tasks)
        path = output / "environment-preparation.json"
        summary = EnvironmentPreparationSummary(
            created_at=datetime.now(UTC),
            source_root=str(config.source_root.expanduser().resolve()),
            environment_root=str(root),
            python_executable=str(config.python_executable.expanduser().resolve()),
            index_url=config.index_url,
            results=results,
            summary_path=str(path),
        )
        path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
        return summary

    def _prepare_one(
        self, task: RealIssueTask, config: EnvironmentPreparationConfig, environment: Path
    ) -> EnvironmentPreparationResult:
        """创建单题环境，并只安装该固定源码声明的依赖。"""
        source = (config.source_root / task.id).expanduser().resolve()
        interpreter = config.python_executable.expanduser().resolve()
        if not interpreter.is_file():
            return EnvironmentPreparationResult(
                task_id=task.id,
                status="interpreter_missing",
                source_commit=task.base_commit,
                logs=(f"interpreter does not exist: {interpreter}",),
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
        if python.is_file() and _marker_matches(marker, task, config.index_url):
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
                    environment_fingerprint=_fingerprint(task, provenance),
                    source_commit=task.base_commit,
                    environment=provenance,
                )
        commands: list[tuple[str, ...]] = []
        logs: list[str] = []
        if not python.is_file():
            create = (str(interpreter), "-m", "venv", str(environment))
            commands.append(create)
            created = _run(create, source.parent, config.timeout_seconds)
            logs.append(_log("create", created))
            if created.returncode != 0:
                return _failed(task, "install_failed", commands, logs)
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
            return _failed(task, "install_failed", commands, logs)
        # 安装固定源码所声明的依赖和项目元数据，但不使用 editable 安装；
        # RunTestsTool 会以工作区和 src/ 为优先导入路径，因此 base/gold 两个
        # 副本均从各自 clone 加载实现，而不是从环境中的固定提交加载。
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
            return _failed(task, status, commands, logs)
        pytest = (str(python), "-m", "pip", "install", "-i", config.index_url, "pytest")
        commands.append(pytest)
        installed_pytest = _run(pytest, source, config.timeout_seconds)
        logs.append(_log("pytest_install", installed_pytest))
        if installed_pytest.returncode != 0:
            return _failed(task, "install_failed", commands, logs)
        try:
            provenance = inspect_test_environment(python)
        except ValueError as exc:
            logs.append(str(exc))
            return _failed(task, "install_failed", commands, logs)
        marker.write_text(
            json.dumps(
                {
                    "task_id": task.id,
                    "base_commit": task.base_commit,
                    "index_url": config.index_url,
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
            environment_fingerprint=_fingerprint(task, provenance),
            source_commit=task.base_commit,
            install_commands=tuple(commands),
            logs=tuple(logs),
            environment=provenance,
        )


def _environment_python(environment: Path) -> Path:
    """兼容 Windows/POSIX venv 布局。"""
    return environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _marker_path(environment: Path) -> Path:
    """返回只在完整安装成功后写入的环境健康标记。"""
    return environment / ".tracefix-environment.json"


def _marker_matches(marker: Path, task: RealIssueTask, index_url: str) -> bool:
    """仅复用同一任务、固定提交和镜像配置已完成的环境。"""
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        payload.get("task_id") == task.id
        and payload.get("base_commit") == task.base_commit
        and payload.get("index_url") == index_url
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
) -> EnvironmentPreparationResult:
    """统一失败结果并保留已执行安装步骤。"""
    return EnvironmentPreparationResult(
        task_id=task.id,
        status=status,
        source_commit=task.base_commit,
        install_commands=tuple(commands),
        logs=tuple(logs),
    )


def _fingerprint(task: RealIssueTask, environment: TestEnvironmentProvenance) -> str:
    """将固定提交和依赖指纹共同纳入环境身份。"""
    payload = json.dumps(
        {
            "task_id": task.id,
            "commit": task.base_commit,
            "environment": environment.fingerprint_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
