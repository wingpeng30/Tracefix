"""来自真实 GitHub Issue 的可追溯任务清单与检出校验。"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracefix.exceptions import BenchmarkError

_PATCH_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$", re.MULTILINE)


def _sha256(path: Path) -> str:
    """按文件原始字节计算摘要，换行变化也会被识别。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise BenchmarkError(
            f"cannot read real task artifact: {exc}",
            context={"path": str(path)},
        ) from exc


def _safe_relative_path(value: str, *, field: str) -> Path:
    """校验任务清单中的仓库相对路径，拒绝目录逃逸和 Git 元数据。"""
    path = Path(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or any(part.casefold() == ".git" for part in path.parts)
    ):
        raise ValueError(f"{field} must contain safe repository-relative paths")
    return path


class RealTaskArtifactHashes(BaseModel):
    """固定问题文本和两个补丁的 SHA-256，防止样本静默漂移。"""

    model_config = ConfigDict(extra="forbid")

    problem_statement: str = Field(pattern=r"^[0-9a-f]{64}$")
    gold_patch: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_patch: str = Field(pattern=r"^[0-9a-f]{64}$")


class RealTaskValidation(BaseModel):
    """一个真实任务的离线或联网检出校验结果。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    artifacts_valid: bool
    source_files: tuple[str, ...]
    test_files: tuple[str, ...]
    checkout_valid: bool | None = None
    checkout_commit: str | None = None
    combined_patch_applicable: bool | None = None


class RealIssueTask(BaseModel):
    """固定上游提交、原始 Issue 和隐藏验收补丁的真实修复任务。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1)
    source_dataset: str = "SWE-bench/SWE-bench_Verified"
    dataset_split: str = "test"
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    repo_url: str
    issue_url: str
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    environment_setup_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    upstream_version: str = Field(min_length=1)
    issue_created_at: datetime
    evaluation_backend: str = "swebench_docker"
    problem_statement_kind: Literal["verbatim", "curated_excerpt"]
    problem_statement_kind: Literal["verbatim", "curated_excerpt"] = "verbatim"
    problem_statement_file: str = "problem.md"
    gold_patch_file: str = "gold.patch"
    test_patch_file: str = "test.patch"
    fail_to_pass: tuple[str, ...] = Field(min_length=1)
    pass_to_pass_count: int = Field(ge=0)
    expected_source_files: tuple[str, ...] = Field(min_length=2)
    expected_test_files: tuple[str, ...] = Field(min_length=1)
    hashes: RealTaskArtifactHashes
    task_dir: Path

    @model_validator(mode="after")
    def validate_manifest(self) -> RealIssueTask:
        """校验 URL、文件数量和所有相对路径，不信任外部数据集内容。"""
        if self.repo_url != f"https://github.com/{self.repo}.git":
            raise ValueError("repo_url must be the canonical HTTPS GitHub clone URL")
        if not self.issue_url.startswith(f"https://github.com/{self.repo}/issues/"):
            raise ValueError("issue_url must belong to the declared repository")
        artifact_paths = (
            self.problem_statement_file,
            self.gold_patch_file,
            self.test_patch_file,
        )
        for value in artifact_paths:
            _safe_relative_path(value, field="artifact file")
        for value in (*self.expected_source_files, *self.expected_test_files):
            _safe_relative_path(value, field="expected files")
        if len(set(self.expected_source_files)) != len(self.expected_source_files):
            raise ValueError("expected_source_files contains duplicate paths")
        if set(self.expected_source_files).intersection(self.expected_test_files):
            raise ValueError("source and hidden-test files must not overlap")
        return self

    @property
    def problem_statement_path(self) -> Path:
        """返回保存原始 Issue 描述的文件。"""
        return (self.task_dir / self.problem_statement_file).resolve()

    @property
    def gold_patch_path(self) -> Path:
        """返回开发者修复补丁；该文件绝不能复制进 Agent 工作区。"""
        return (self.task_dir / self.gold_patch_file).resolve()

    @property
    def test_patch_path(self) -> Path:
        """返回隐藏验收补丁；只允许在 Agent 结束后由评测器应用。"""
        return (self.task_dir / self.test_patch_file).resolve()

    @property
    def problem_statement(self) -> str:
        """读取向 Agent 展示的 Issue 文本；是否节选由清单明确标记。"""
        try:
            return self.problem_statement_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BenchmarkError(
                f"cannot read real task problem statement: {exc}",
                context={"task_id": self.id},
            ) from exc

    @classmethod
    def load(cls, task_dir: str | Path) -> RealIssueTask:
        """从任务目录加载清单，并立即验证文件边界、摘要和补丁路径。"""
        root = Path(task_dir).expanduser().resolve()
        manifest = root / "task.json"
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            task = cls.model_validate({**payload, "task_dir": root})
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise BenchmarkError(
                f"invalid real task manifest: {exc}",
                context={"manifest": str(manifest)},
            ) from exc

        # 解析符号链接后的真实路径必须仍处于任务目录，字符串级校验不是充分条件。
        for label, path in {
            "problem_statement": task.problem_statement_path,
            "gold_patch": task.gold_patch_path,
            "test_patch": task.test_patch_path,
        }.items():
            if not path.is_relative_to(root) or not path.is_file():
                raise BenchmarkError(
                    "real task artifact is missing or escapes its task directory",
                    context={"task_id": task.id, "field": label, "path": str(path)},
                )
        task.validate_artifacts()
        return task

    @staticmethod
    def _patch_paths(path: Path) -> tuple[str, ...]:
        """提取标准 Git diff 的目标路径，并拒绝改名或不安全路径。"""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BenchmarkError(f"cannot read real task patch: {exc}") from exc
        matches = _PATCH_HEADER.findall(text)
        if not matches:
            raise BenchmarkError(
                "real task patch has no git diff headers", context={"path": str(path)}
            )
        result: list[str] = []
        for before, after in matches:
            if before != after:
                raise BenchmarkError(
                    "renames are not supported in real task artifacts",
                    context={"before": before, "after": after},
                )
            try:
                _safe_relative_path(after, field="patch path")
            except ValueError as exc:
                raise BenchmarkError(str(exc), context={"path": after}) from exc
            result.append(Path(after).as_posix())
        return tuple(result)

    def validate_artifacts(self) -> RealTaskValidation:
        """离线验证三个文件的哈希以及 gold/test patch 的准确文件集合。"""
        actual_hashes = RealTaskArtifactHashes(
            problem_statement=_sha256(self.problem_statement_path),
            gold_patch=_sha256(self.gold_patch_path),
            test_patch=_sha256(self.test_patch_path),
        )
        if actual_hashes != self.hashes:
            raise BenchmarkError(
                "real task artifact checksum mismatch",
                context={"task_id": self.id},
            )
        source_files = self._patch_paths(self.gold_patch_path)
        test_files = self._patch_paths(self.test_patch_path)
        if source_files != self.expected_source_files:
            raise BenchmarkError(
                "gold patch files do not match manifest",
                context={"task_id": self.id, "actual": source_files},
            )
        if test_files != self.expected_test_files:
            raise BenchmarkError(
                "test patch files do not match manifest",
                context={"task_id": self.id, "actual": test_files},
            )
        return RealTaskValidation(
            task_id=self.id,
            artifacts_valid=True,
            source_files=source_files,
            test_files=test_files,
        )

    def prepare_checkout(self, destination: str | Path) -> Path:
        """克隆上游仓库并检出固定 base commit，禁止复用已有目录。"""
        target = Path(destination).expanduser().resolve()
        if target.exists():
            raise BenchmarkError(
                "real task checkout destination already exists",
                context={"task_id": self.id, "path": str(target)},
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        # --no-hardlinks 让本地 URL 测试和真实 GitHub 克隆都获得独立对象存储。
        self._run_git(
            ["clone", "--quiet", "--no-hardlinks", "--no-checkout", self.repo_url, str(target)],
            cwd=target.parent,
            timeout=300,
        )
        self._run_git(
            ["checkout", "--quiet", "--detach", self.base_commit],
            cwd=target,
            timeout=120,
        )
        head = self._run_git(["rev-parse", "HEAD"], cwd=target).strip()
        if head != self.base_commit:
            raise BenchmarkError(
                "real task checkout resolved to an unexpected commit",
                context={"task_id": self.id, "expected": self.base_commit, "actual": head},
            )
        return target

    def validate_checkout(self, checkout: str | Path) -> RealTaskValidation:
        """确认检出提交正确，并联合预检隐藏测试与开发者补丁可以应用。"""
        root = Path(checkout).expanduser().resolve()
        head = self._run_git(["rev-parse", "HEAD"], cwd=root).strip()
        if head != self.base_commit:
            raise BenchmarkError(
                "checkout does not match real task base commit",
                context={"task_id": self.id, "expected": self.base_commit, "actual": head},
            )
        # 两个补丁一次性交给 git apply --check，才能证明组合后也不存在上下文冲突。
        self._run_git(
            ["apply", "--check", str(self.test_patch_path), str(self.gold_patch_path)],
            cwd=root,
            timeout=120,
        )
        offline = self.validate_artifacts()
        return offline.model_copy(
            update={
                "checkout_valid": True,
                "checkout_commit": head,
                "combined_patch_applicable": True,
            }
        )

    def apply_hidden_tests(self, workspace: str | Path) -> None:
        """在 Agent 结束后应用隐藏测试补丁；不执行测试或应用 gold patch。"""
        root = Path(workspace).expanduser().resolve()
        # 先预检再写入，失败时保持工作区不变。
        self._run_git(
            ["apply", "--check", str(self.test_patch_path)], cwd=root, timeout=120
        )
        self._run_git(["apply", str(self.test_patch_path)], cwd=root, timeout=120)

    @staticmethod
    def _run_git(arguments: list[str], *, cwd: Path, timeout: int = 60) -> str:
        """以 shell=False 执行固定 Git 参数，并把错误转为稳定基准异常。"""
        try:
            result = subprocess.run(
                ["git", *arguments],
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
            raise BenchmarkError(
                f"real task git command failed to start: {exc}"
            ) from exc
        if result.returncode != 0:
            raise BenchmarkError(
                "real task git command failed",
                context={
                    "returncode": result.returncode,
                    "stderr": result.stderr.strip(),
                    "arguments": arguments,
                },
            )
        return result.stdout


def load_real_issue_tasks(
    tasks_dir: str | Path,
    *,
    task_ids: tuple[str, ...] = (),
) -> tuple[RealIssueTask, ...]:
    """按 ID 稳定加载真实 Issue 任务，并拒绝未知筛选值。"""
    root = Path(tasks_dir).expanduser().resolve()
    if not root.is_dir():
        raise BenchmarkError(
            "real tasks directory does not exist", context={"path": str(root)}
        )
    selected = set(task_ids)
    tasks = tuple(
        RealIssueTask.load(path)
        for path in sorted(root.iterdir(), key=lambda item: item.name)
        if path.is_dir()
        and (path / "task.json").is_file()
        and (not selected or path.name in selected)
    )
    missing = selected.difference(task.id for task in tasks)
    if missing:
        raise BenchmarkError(
            "unknown real task IDs", context={"missing_task_ids": sorted(missing)}
        )
    if not tasks:
        raise BenchmarkError("no real issue tasks were selected", context={"path": str(root)})
    return tasks
