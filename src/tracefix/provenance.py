"""实验运行所需的代码、任务、模型和依赖溯源信息。"""

from __future__ import annotations

import hashlib
import platform
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from tracefix._version import __version__
from tracefix.exceptions import sanitize_payload
from tracefix.models import LLMConfig


class RunProvenance(BaseModel):
    """足以判断两次实验是否可比较的最小运行清单。"""

    model_config = ConfigDict(extra="forbid")

    tracefix_version: str
    tracefix_commit: str | None = None
    tracefix_worktree_dirty: bool | None = None
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_parameters: dict[str, JsonValue]
    python_version: str
    platform: str
    dependency_versions: dict[str, str | None]


def task_sha256(task: str) -> str:
    """按实际传给 Agent 的 UTF-8 任务文本计算稳定 SHA-256。"""
    return hashlib.sha256(task.encode("utf-8")).hexdigest()


def collect_run_provenance(
    task: str,
    llm_config: LLMConfig,
    *,
    project_root: Path | None = None,
) -> RunProvenance:
    """采集 TraceFix Git 状态、模型参数和直接运行依赖版本。

    安装后的 wheel 可能不带 ``.git``；此时 commit 和 dirty 明确记为 ``null``，
    不伪造版本信息，也不阻断正常任务。
    """
    root = project_root or Path(__file__).resolve().parents[2]
    commit, dirty = _read_git_state(root)
    parameters = sanitize_payload(llm_config.model_dump(mode="json"))
    assert isinstance(parameters, dict)
    return RunProvenance(
        tracefix_version=__version__,
        tracefix_commit=commit,
        tracefix_worktree_dirty=dirty,
        task_sha256=task_sha256(task),
        model_parameters=parameters,
        python_version=platform.python_version(),
        platform=f"{platform.system()}-{platform.release()}-{platform.machine()}",
        dependency_versions={
            "pydantic": _package_version("pydantic"),
            "litellm": _package_version("litellm"),
            "python-dotenv": _package_version("python-dotenv"),
        },
    )


def _package_version(distribution: str) -> str | None:
    """读取已安装分发版本；可选依赖缺失时保留明确的空值。"""
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None


def _read_git_state(root: Path) -> tuple[str | None, bool | None]:
    """只读查询 TraceFix 自身仓库，不把失败误报为干净状态。"""
    try:
        root_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            shell=False,
        )
        # 临时目录可能位于另一个 Git 仓库内部，不能把外层版本误记为 TraceFix。
        detected_root = Path(root_result.stdout.strip()).resolve()
        if root_result.returncode != 0 or detected_root != root.resolve():
            return None, None
        commit_result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            shell=False,
        )
        if commit_result.returncode != 0:
            return None, None
        status_result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            shell=False,
        )
        if status_result.returncode != 0:
            return commit_result.stdout.strip() or None, None
        return commit_result.stdout.strip() or None, bool(status_result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None, None
