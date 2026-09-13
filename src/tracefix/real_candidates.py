"""SWE-bench Verified 候选任务的离线收集与结构筛选。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tracefix.exceptions import BenchmarkError

_DIFF_PATH = re.compile(r"^diff --git a/(.+) b/(.+)$", re.MULTILINE)
_PYTHON = {".py", ".pyi"}
_REPOSITORIES = (
    "pytest-dev/pytest",
    "pylint-dev/pylint",
    "sphinx-doc/sphinx",
    "psf/requests",
)


class CandidateCollectionConfig(BaseModel):
    """候选下载与仓库配额配置。"""

    model_config = ConfigDict(extra="forbid")

    dataset: str = "SWE-bench/SWE-bench_Verified"
    revision: str = "main"
    source: Path | None = None
    output_dir: Path = Path("benchmarks/real_candidates")
    per_repository: int = Field(default=3, ge=1)
    repositories: tuple[str, ...] = _REPOSITORIES


class CandidateRecord(BaseModel):
    """一条候选任务的脱敏结构记录。"""

    model_config = ConfigDict(extra="forbid")

    instance_id: str
    repo: str
    base_commit: str
    version: str
    problem_statement_sha256: str
    patch_sha256: str
    test_patch_sha256: str
    source_files: tuple[str, ...]
    test_files: tuple[str, ...]
    related_files: tuple[str, ...] = ()
    source_file_count: int = Field(ge=0)
    test_file_count: int = Field(ge=0)
    eligible: bool
    exclusion_reasons: tuple[str, ...] = ()


class CandidateCollectionResult(BaseModel):
    """候选池收集的可复现摘要。"""

    model_config = ConfigDict(extra="forbid")

    dataset: str
    revision: str
    created_at: datetime
    requested_repositories: tuple[str, ...]
    per_repository: int
    selected: tuple[CandidateRecord, ...]
    excluded: tuple[CandidateRecord, ...]
    output_path: str


def _sha(text: str) -> str:
    """对任务文本或补丁计算稳定 SHA-256。"""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def patch_paths(patch: str) -> tuple[str, ...]:
    """解析 unified diff 路径并拒绝绝对路径、目录逃逸和改名。"""
    paths: list[str] = []
    for before, after in _DIFF_PATH.findall(patch):
        if before != after or after.startswith(("/", "../")) or ".git" in Path(after).parts:
            continue
        normalized = Path(after).as_posix()
        if normalized not in paths:
            paths.append(normalized)
    return tuple(paths)


def _load_rows(config: CandidateCollectionConfig) -> tuple[dict[str, Any], ...]:
    """读取本地 JSON/JSONL 或通过可选 datasets 包下载固定 revision。"""
    if config.source:
        path = config.source.expanduser().resolve()
        try:
            text = path.read_text(encoding="utf-8")
            if path.suffix.casefold() == ".jsonl":
                return tuple(json.loads(line) for line in text.splitlines() if line.strip())
            payload = json.loads(text)
            return tuple(payload if isinstance(payload, list) else payload["data"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise BenchmarkError(
                "invalid local SWE-bench source", context={"path": str(path)}
            ) from exc
    try:
        from datasets import load_dataset  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BenchmarkError(
            "collecting from Hugging Face requires the optional benchmark dependency 'datasets'"
        ) from exc
    try:
        dataset = load_dataset(config.dataset, revision=config.revision, split="test")
        return tuple(dict(row) for row in dataset)
    except Exception as exc:  # noqa: BLE001 - third-party errors are normalized at the boundary
        raise BenchmarkError(
            "cannot download SWE-bench dataset",
            context={"dataset": config.dataset, "revision": config.revision},
        ) from exc


def _record(row: dict[str, Any], repositories: set[str]) -> CandidateRecord:
    """把官方实例转换为脱敏结构记录，不保存模型响应。"""
    repo = str(row.get("repo", ""))
    instance = str(row.get("instance_id", ""))
    problem = str(row.get("problem_statement", ""))
    patch = str(row.get("patch", ""))
    test_patch = str(row.get("test_patch", ""))
    source = tuple(
        path
        for path in patch_paths(patch)
        if Path(path).suffix.casefold() in _PYTHON and not path.startswith("tests/")
    )
    tests = tuple(
        path
        for path in patch_paths(test_patch)
        if path.startswith(("test", "tests/")) or "/test" in path
    )
    related = tuple(
        dict.fromkeys((*source, *tests, *(str(x) for x in row.get("related_files", ()))))
    )
    reasons: list[str] = []
    if repo not in repositories:
        reasons.append("repository_not_requested")
    if not problem.strip():
        reasons.append("empty_problem_statement")
    if not patch.strip() or not test_patch.strip():
        reasons.append("missing_patch_or_test_patch")
    if len(source) < 2:
        reasons.append("fewer_than_two_python_source_files")
    if not tests:
        reasons.append("no_test_patch_files")
    return CandidateRecord(
        instance_id=instance,
        repo=repo,
        base_commit=str(row.get("base_commit", "")),
        version=str(row.get("version", "unknown")),
        problem_statement_sha256=_sha(problem),
        patch_sha256=_sha(patch),
        test_patch_sha256=_sha(test_patch),
        source_files=source,
        test_files=tests,
        related_files=related,
        source_file_count=len(source),
        test_file_count=len(tests),
        eligible=not reasons,
        exclusion_reasons=tuple(reasons),
    )


def collect_candidates(config: CandidateCollectionConfig) -> CandidateCollectionResult:
    """按仓库配额稳定选择候选，并写出不含补丁正文的清单。"""
    rows = tuple(sorted(_load_rows(config), key=lambda row: str(row.get("instance_id", ""))))
    records = tuple(_record(row, set(config.repositories)) for row in rows)
    selected: list[CandidateRecord] = []
    counts = {repo: 0 for repo in config.repositories}
    for record in records:
        if (
            record.eligible
            and record.repo in counts
            and counts[record.repo] < config.per_repository
        ):
            selected.append(record)
            counts[record.repo] += 1
    missing = [repo for repo, count in counts.items() if count < config.per_repository]
    if missing:
        raise BenchmarkError(
            "candidate quota is not satisfied",
            context={"missing_repositories": missing, "counts": counts},
        )
    output = config.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    # 保存问题和补丁原文供第二阶段准备源码使用；模型轨迹和 API 响应不会进入候选目录。
    rows_by_id = {str(row.get("instance_id", "")): row for row in rows}
    for record in selected:
        row = rows_by_id[record.instance_id]
        task_dir = output / record.instance_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "problem.md").write_text(
            str(row.get("problem_statement", "")), encoding="utf-8"
        )
        (task_dir / "gold.patch").write_text(str(row.get("patch", "")), encoding="utf-8")
        (task_dir / "test.patch").write_text(str(row.get("test_patch", "")), encoding="utf-8")
        (task_dir / "candidate.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    path = output / "candidate-pool.json"
    result = CandidateCollectionResult(
        dataset=config.dataset,
        revision=config.revision,
        created_at=datetime.now(UTC),
        requested_repositories=config.repositories,
        per_repository=config.per_repository,
        selected=tuple(selected),
        excluded=tuple(record for record in records if record not in selected),
        output_path=str(path),
    )
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result
