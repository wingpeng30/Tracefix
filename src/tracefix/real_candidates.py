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
    min_source_files: int = Field(default=1, ge=1)
    repositories: tuple[str, ...] = _REPOSITORIES
    holdout_mode: bool = False
    selection_seed: int = 20260921
    excluded_task_ids: tuple[str, ...] = ()


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
    selection_rank: int | None = None
    duplicate_of: str | None = None


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
    schema_version: int = 2
    source_sha256: str | None = None
    selection_seed: int | None = None
    excluded_task_ids: tuple[str, ...] = ()
    candidate_order: tuple[str, ...] = ()
    candidate_order_sha256: str | None = None


def _normalized_text(text: str) -> str:
    """统一工件换行符，避免 Windows 文本写入将混合换行符变形。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _sha(text: str) -> str:
    """对任务文本或补丁计算稳定 SHA-256。"""
    normalized = _normalized_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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


def _rows_hash(rows: tuple[dict[str, Any], ...]) -> str:
    """Hash the normalized source rows without depending on input file formatting."""
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _selection_key(seed: int, record: CandidateRecord) -> str:
    value = f"{seed}:{record.repo}:{record.instance_id}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _record(row: dict[str, Any], repositories: set[str], min_source_files: int) -> CandidateRecord:
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
    related_values = list(
        dict.fromkeys((*source, *tests, *(str(x) for x in row.get("related_files", ()))))
    )
    # 不凭空加入 __init__.py 等推测路径；第二阶段检出后再由 Repo Map 扩展真实邻居。
    related = tuple(related_values[:10])
    reasons: list[str] = []
    if repo not in repositories:
        reasons.append("repository_not_requested")
    if not problem.strip():
        reasons.append("empty_problem_statement")
    if not patch.strip() or not test_patch.strip():
        reasons.append("missing_patch_or_test_patch")
    if len(source) < min_source_files:
        reasons.append(f"fewer_than_{min_source_files}_python_source_files")
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


def _write_task_manifest(task_dir: Path, row: dict[str, Any], record: CandidateRecord) -> None:
    """把候选结构记录转换为 RealIssueTask 可加载的最小清单。"""
    issue_number = record.instance_id.rsplit("-", 1)[-1]
    problem = str(row.get("problem_statement", ""))
    # RealIssueTask 会逐一校验 gold patch 的全部路径；这里不能只保留 Python
    # 文件，否则同时修改 setup.cfg 等配置的真实任务无法通过工件验收。
    gold_files = patch_paths(str(row.get("patch", "")))
    related_files = tuple(dict.fromkeys((*gold_files, *record.related_files)))[:10]
    pass_to_pass = row.get("PASS_TO_PASS", row.get("pass_to_pass_count", 0))
    # SWE-bench 官方字段是测试选择器列表；TraceFix 清单只需要其数量。
    pass_to_pass_count = (
        len(pass_to_pass) if isinstance(pass_to_pass, list) else int(pass_to_pass or 0)
    )
    fail_to_pass = tuple(
        str(item) for item in row.get("FAIL_TO_PASS", row.get("fail_to_pass", ()))
    )
    # 资格验收优先运行数据集明确声明的目标用例；缺少选择器的 fixture 才回退。
    test_command = "pytest -q" if not fail_to_pass else "pytest -q " + " ".join(fail_to_pass)
    payload = {
        "id": record.instance_id,
        "title": problem.splitlines()[0][:200] or record.instance_id,
        "source_dataset": "SWE-bench/SWE-bench_Verified",
        "dataset_split": "test",
        "repo": record.repo,
        "repo_url": f"https://github.com/{record.repo}.git",
        "issue_url": f"https://github.com/{record.repo}/issues/{issue_number}",
        "base_commit": record.base_commit,
        "environment_setup_commit": str(row.get("environment_setup_commit", record.base_commit)),
        "upstream_version": record.version,
        "issue_created_at": str(row.get("created_at", "1970-01-01T00:00:00Z")),
        "evaluation_backend": "swebench_docker",
        "problem_statement_kind": "verbatim",
        "problem_statement_file": "problem.md",
        "gold_patch_file": "gold.patch",
        "test_patch_file": "test.patch",
        "fail_to_pass": list(fail_to_pass) or ["tests"],
        "test_command": test_command,
        "pass_to_pass_count": pass_to_pass_count,
        "expected_source_files": list(gold_files),
        "expected_test_files": list(record.test_files),
        "related_context_files": list(related_files),
        "hashes": {
            "problem_statement": record.problem_statement_sha256,
            "gold_patch": record.patch_sha256,
            "test_patch": record.test_patch_sha256,
        },
    }
    (task_dir / "task.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def collect_candidates(config: CandidateCollectionConfig) -> CandidateCollectionResult:
    """按仓库配额稳定选择候选，并写出不含补丁正文的清单。"""
    rows = tuple(sorted(_load_rows(config), key=lambda row: str(row.get("instance_id", ""))))
    records = tuple(_record(row, set(config.repositories), config.min_source_files) for row in rows)
    excluded_ids = set(config.excluded_task_ids)
    seen_problem: dict[tuple[str, str], str] = {}
    seen_patch: dict[tuple[str, str], str] = {}
    reviewed: list[CandidateRecord] = []
    for record in records:
        reasons = list(record.exclusion_reasons)
        duplicate_of = None
        if record.instance_id in excluded_ids:
            reasons.append("previously_reviewed_task")
        problem_key = (record.repo, record.problem_statement_sha256)
        patch_key = (record.repo, record.patch_sha256)
        if config.holdout_mode and problem_key in seen_problem:
            duplicate_of = seen_problem[problem_key]
            reasons.append("duplicate_problem_statement")
        elif config.holdout_mode and patch_key in seen_patch:
            duplicate_of = seen_patch[patch_key]
            reasons.append("duplicate_patch")
        else:
            seen_problem[problem_key] = record.instance_id
            seen_patch[patch_key] = record.instance_id
        reviewed.append(
            record.model_copy(
                update={
                    "eligible": not reasons,
                    "exclusion_reasons": tuple(dict.fromkeys(reasons)),
                    "duplicate_of": duplicate_of,
                }
            )
        )
    records = tuple(reviewed)
    ordered = tuple(
        sorted(
            (record for record in records if record.eligible),
            key=lambda item: (_selection_key(config.selection_seed, item), item.instance_id),
        )
        if config.holdout_mode
        else records
    )
    selected: list[CandidateRecord] = []
    counts = {repo: 0 for repo in config.repositories}
    candidate_order: list[str] = []
    ranks = {record.instance_id: index for index, record in enumerate(ordered, start=1)}
    for record in ordered:
        if record.eligible and record.repo in counts:
            candidate_order.append(record.instance_id)
        if (
            record.eligible
            and record.repo in counts
            and (config.holdout_mode or counts[record.repo] < config.per_repository)
        ):
            selected.append(record.model_copy(update={"selection_rank": ranks[record.instance_id]}))
            counts[record.repo] += 1
    output = config.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    # 保存问题和补丁原文供第二阶段准备源码使用；模型轨迹和 API 响应不会进入候选目录。
    rows_by_id = {str(row.get("instance_id", "")): row for row in rows}
    for record in selected:
        row = rows_by_id[record.instance_id]
        task_dir = output / record.instance_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "problem.md").write_text(
            _normalized_text(str(row.get("problem_statement", ""))), encoding="utf-8"
        )
        (task_dir / "gold.patch").write_text(
            _normalized_text(str(row.get("patch", ""))), encoding="utf-8"
        )
        (task_dir / "test.patch").write_text(
            _normalized_text(str(row.get("test_patch", ""))), encoding="utf-8"
        )
        (task_dir / "candidate.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
        _write_task_manifest(task_dir, row, record)
    path = output / "candidate-pool.json"
    portable_output_path = (config.output_dir / "candidate-pool.json").as_posix()
    order_hash = _sha("\n".join(candidate_order))
    result = CandidateCollectionResult(
        dataset=config.dataset,
        revision=config.revision,
        created_at=datetime.now(UTC),
        requested_repositories=config.repositories,
        per_repository=config.per_repository,
        selected=tuple(selected),
        excluded=tuple(
            record
            for record in records
            if record.instance_id not in {item.instance_id for item in selected}
        ),
        output_path=portable_output_path,
        source_sha256=_rows_hash(rows),
        selection_seed=config.selection_seed if config.holdout_mode else None,
        excluded_task_ids=tuple(sorted(excluded_ids)),
        candidate_order=tuple(candidate_order),
        candidate_order_sha256=order_hash,
    )
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    available_counts = {
        repo: sum(record.eligible and record.repo == repo for record in records)
        for repo in config.repositories
    }
    missing = [repo for repo, count in available_counts.items() if count < config.per_repository]
    if missing:
        # 即使配额失败也持久化报告，避免用户只能得到一条无法诊断的错误消息。
        raise BenchmarkError(
            "candidate quota is not satisfied",
            context={
                "missing_repositories": missing,
                "counts": available_counts,
                "report_path": str(path),
            },
        )
    return result
