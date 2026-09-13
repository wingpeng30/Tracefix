"""不调用 LLM 的 Repo Map 文件定位评测。"""

from __future__ import annotations

import math
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from tracefix.exceptions import BenchmarkError
from tracefix.real_benchmark import RealIssueTask, load_real_issue_tasks
from tracefix.repository import RepoMapConfig, RepositoryIndexer
from tracefix.repository.indexer import IndexedFile

_WORDS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_K_VALUES = (1, 3, 5)


class RetrievalEvaluationConfig(BaseModel):
    """离线 Repo Map 评测所需的任务、固定检出和输出位置。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    tasks_dir: Path = Path("benchmarks/real_tasks")
    source_root: Path = Path("runs/real-task-validation")
    output_dir: Path = Path("runs")
    task_ids: tuple[str, ...] = ()
    repo_map: RepoMapConfig = Field(default_factory=RepoMapConfig)


class RetrievalMetrics(BaseModel):
    """单个排序结果或聚合结果的标准文件定位指标。"""

    model_config = ConfigDict(extra="forbid")

    hit_at_1: float = Field(ge=0, le=1)
    hit_at_3: float = Field(ge=0, le=1)
    hit_at_5: float = Field(ge=0, le=1)
    recall_at_1: float = Field(ge=0, le=1)
    recall_at_3: float = Field(ge=0, le=1)
    recall_at_5: float = Field(ge=0, le=1)
    mrr: float = Field(ge=0, le=1)


class RetrievalTaskResult(BaseModel):
    """一题的两种排序结果；gold patch 仅在此模型中作为离线标签出现。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    base_commit: str
    task_sha256: str
    gold_patch_target_files: tuple[str, ...]
    baseline_candidates: tuple[str, ...]
    repo_map_candidates: tuple[str, ...]
    baseline_metrics: RetrievalMetrics
    repo_map_metrics: RetrievalMetrics
    index_duration_ms: float = Field(ge=0)
    indexed_file_count: int = Field(ge=0)
    indexed_symbol_count: int = Field(ge=0)
    skipped_file_count: int = Field(ge=0)
    repo_map_chars: int = Field(ge=0)
    repo_map_estimated_tokens: int = Field(ge=0)
    graph_expanded_file_count: int = Field(ge=0)


class RetrievalEvaluationSummary(BaseModel):
    """一次离线评测的可复现摘要，不包含任何模型响应或 API 成本。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str
    created_at: datetime
    tasks_dir: str
    source_root: str
    task_count: int = Field(ge=1)
    repo_map_config: RepoMapConfig
    baseline_metrics: RetrievalMetrics
    repo_map_metrics: RetrievalMetrics
    average_index_duration_ms: float = Field(ge=0)
    average_repo_map_estimated_tokens: float = Field(ge=0)
    results: tuple[RetrievalTaskResult, ...]
    summary_path: str


class RetrievalEvaluator:
    """在固定源码检出上评测关键词基线与 Repo Map，不调用模型或测试命令。"""

    def run(self, config: RetrievalEvaluationConfig) -> RetrievalEvaluationSummary:
        """执行评测并写出 JSON；索引器从未接触 gold patch 内容。"""
        tasks = load_real_issue_tasks(config.tasks_dir, task_ids=config.task_ids)
        run_id = f"retrieval-eval-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        output_dir = config.output_dir.expanduser().resolve() / run_id
        output_dir.mkdir(parents=True, exist_ok=False)
        results = tuple(
            self._evaluate_task(task, config.source_root, config.repo_map) for task in tasks
        )
        summary_path = output_dir / "summary.json"
        summary = RetrievalEvaluationSummary(
            run_id=run_id,
            created_at=datetime.now(UTC),
            tasks_dir=str(config.tasks_dir.expanduser().resolve()),
            source_root=str(config.source_root.expanduser().resolve()),
            task_count=len(results),
            repo_map_config=config.repo_map.model_copy(deep=True),
            baseline_metrics=_average_metrics(item.baseline_metrics for item in results),
            repo_map_metrics=_average_metrics(item.repo_map_metrics for item in results),
            average_index_duration_ms=(
                sum(item.index_duration_ms for item in results) / len(results)
            ),
            average_repo_map_estimated_tokens=(
                sum(item.repo_map_estimated_tokens for item in results) / len(results)
            ),
            results=results,
            summary_path=str(summary_path),
        )
        summary_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
        return summary

    @staticmethod
    def _evaluate_task(
        task: RealIssueTask,
        source_root: Path,
        repo_map_config: RepoMapConfig,
    ) -> RetrievalTaskResult:
        """验证固定提交，构建索引并将两种候选排序与 gold 文件集合比较。"""
        workspace = (source_root.expanduser().resolve() / task.id).resolve()
        _validate_checkout_commit(workspace, task)

        # validate_artifacts 从 gold patch 的 diff header 得到标签文件；只取路径集合，
        # 不会将补丁文本传递给 Indexer、Repo Map 或任一排序算法。
        targets = task.validate_artifacts().source_files
        indexer = RepositoryIndexer(workspace, repo_map_config)
        started = time.perf_counter()
        index = indexer.build()
        repo_map = indexer.make_repo_map(index, task.problem_statement)
        duration_ms = (time.perf_counter() - started) * 1000
        baseline = _filename_keyword_ranking(
            index.files,
            task.problem_statement,
            limit=repo_map_config.max_candidate_files,
        )
        return RetrievalTaskResult(
            task_id=task.id,
            base_commit=task.base_commit,
            task_sha256=task.hashes.problem_statement,
            gold_patch_target_files=targets,
            baseline_candidates=baseline,
            repo_map_candidates=repo_map.candidate_files,
            baseline_metrics=_rank_metrics(baseline, targets),
            repo_map_metrics=_rank_metrics(repo_map.candidate_files, targets),
            index_duration_ms=duration_ms,
            indexed_file_count=len(index.files),
            indexed_symbol_count=index.symbol_count,
            skipped_file_count=len(index.skipped_files),
            repo_map_chars=len(repo_map.text),
            repo_map_estimated_tokens=math.ceil(len(repo_map.text.encode("utf-8")) / 4),
            graph_expanded_file_count=repo_map.graph_expanded_file_count,
        )


def _validate_checkout_commit(workspace: Path, task: RealIssueTask) -> None:
    """拒绝缺失或漂移的源码检出，避免标签和候选来自不同版本。"""
    if not workspace.is_dir():
        raise BenchmarkError(
            "retrieval evaluation source checkout is missing",
            context={"task_id": task.id, "workspace": str(workspace)},
        )
    try:
        result = subprocess.run(
            ["git", "-c", "core.longpaths=true", "rev-parse", "HEAD"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BenchmarkError(
            "cannot inspect retrieval evaluation checkout",
            context={"task_id": task.id, "workspace": str(workspace)},
        ) from exc
    actual = result.stdout.strip()
    if result.returncode != 0 or actual != task.base_commit:
        raise BenchmarkError(
            "retrieval evaluation checkout does not match task base commit",
            context={"task_id": task.id, "expected": task.base_commit, "actual": actual},
        )


def _filename_keyword_ranking(
    files: tuple[IndexedFile, ...], task: str, *, limit: int = 12
) -> tuple[str, ...]:
    """只以任务词和文件路径排序，作为不依赖 AST、导入或符号的朴素对照。"""
    tokens = {word.casefold() for word in _WORDS.findall(task)}

    def score(item: IndexedFile) -> tuple[int, str]:
        path = item.path
        path_terms = {word.casefold() for word in _WORDS.findall(path)}
        return (-len(tokens.intersection(path_terms)), path)

    source_files = [item for item in files if not item.is_test]
    return tuple(item.path for item in sorted(source_files, key=score)[:limit])


def _rank_metrics(candidates: tuple[str, ...], targets: tuple[str, ...]) -> RetrievalMetrics:
    """计算命中、召回与首个相关文件排名；不存在目标时视为任务工件错误。"""
    target_set = set(targets)
    if not target_set:
        raise BenchmarkError("retrieval evaluation requires at least one gold target")
    positions = [index for index, path in enumerate(candidates, start=1) if path in target_set]

    def hit_at(k: int) -> float:
        return float(any(position <= k for position in positions))

    def recall_at(k: int) -> float:
        return len(set(candidates[:k]).intersection(target_set)) / len(target_set)

    return RetrievalMetrics(
        hit_at_1=hit_at(1),
        hit_at_3=hit_at(3),
        hit_at_5=hit_at(5),
        recall_at_1=recall_at(1),
        recall_at_3=recall_at(3),
        recall_at_5=recall_at(5),
        mrr=1 / min(positions) if positions else 0.0,
    )


def _average_metrics(metrics: object) -> RetrievalMetrics:
    """对任务级指标做未加权算术平均，避免大仓库主导结果。"""
    values = tuple(metrics)
    if not values:
        raise BenchmarkError("cannot average an empty retrieval evaluation")
    return RetrievalMetrics(
        **{
            name: sum(getattr(value, name) for value in values) / len(values)
            for name in RetrievalMetrics.model_fields
        }
    )
