"""离线检索评测的指标与 gold 标签隔离测试。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.exceptions import BenchmarkError
from tracefix.repository import RepoMap, RepositoryIndex
from tracefix.repository.indexer import IndexedFile
from tracefix.retrieval_eval import (
    RetrievalEvaluationConfig,
    RetrievalEvaluator,
    _average_metrics,
    _filename_keyword_ranking,
    _rank_metrics,
    _validate_checkout_commit,
)


def test_rank_metrics_handles_multiple_gold_targets() -> None:
    """命中与召回应针对多文件补丁分别计算。"""
    metrics = _rank_metrics(("pkg/noise.py", "pkg/a.py", "pkg/b.py"), ("pkg/a.py", "pkg/b.py"))

    assert metrics.hit_at_1 == 0
    assert metrics.hit_at_3 == 1
    assert metrics.recall_at_1 == 0
    assert metrics.recall_at_3 == 1
    assert metrics.mrr == 0.5


def test_filename_baseline_does_not_use_symbols_or_imports() -> None:
    """朴素对照只能看到路径名，保证与 Repo Map 的信息边界不同。"""
    parser = IndexedFile(path="src/parser.py", module="parser")
    hidden = IndexedFile(path="src/implementation.py", module="implementation", imports=("parser",))
    test_file = IndexedFile(path="tests/test_parser.py", module="test_parser", is_test=True)

    ranked = _filename_keyword_ranking((hidden, test_file, parser), "parser bug")

    assert ranked == ("src/parser.py", "src/implementation.py")


def test_evaluator_writes_summary_without_reading_gold_patch_content(
    tmp_path, monkeypatch
) -> None:
    """运行级测试通过 fake task 验证标签仅以 source_files 形式进入评测。"""
    task = SimpleNamespace(
        id="owner__repo-1",
        base_commit="a" * 40,
        hashes=SimpleNamespace(problem_statement="b" * 64),
        problem_statement="parser empty input",
        validate_artifacts=lambda: SimpleNamespace(source_files=("pkg/parser.py",)),
    )
    seen = []

    class FakeIndexer:
        def __init__(self, workspace, config) -> None:
            seen.append((workspace, config))

        def build(self):
            return RepositoryIndex(
                files=(
                    IndexedFile(path="pkg/parser.py", module="parser"),
                    IndexedFile(path="pkg/noise.py", module="noise"),
                )
            )

        def make_repo_map(self, index, task_text):
            assert task_text == "parser empty input"
            return RepoMap(
                text="[TraceFix Repository Map]\n- pkg/parser.py",
                candidate_files=("pkg/parser.py",),
                indexed_file_count=len(index.files),
                symbol_count=0,
                skipped_file_count=0,
            )

    monkeypatch.setattr(
        "tracefix.retrieval_eval.load_real_issue_tasks",
        lambda *_args, **_kwargs: (task,),
    )
    monkeypatch.setattr("tracefix.retrieval_eval.RepositoryIndexer", FakeIndexer)
    monkeypatch.setattr("tracefix.retrieval_eval._validate_checkout_commit", lambda *_args: None)

    summary = RetrievalEvaluator().run(
        RetrievalEvaluationConfig(
            tasks_dir=tmp_path / "tasks",
            source_root=tmp_path / "source",
            output_dir=tmp_path / "output",
        )
    )

    assert summary.repo_map_metrics.hit_at_1 == 1
    assert summary.baseline_metrics.hit_at_1 == 1
    assert Path(summary.summary_path).is_file()
    assert len(seen) == 1


def test_checkout_validation_rejects_missing_and_wrong_commit(tmp_path, monkeypatch) -> None:
    """固定提交是离线比较的前提，目录缺失或版本漂移必须中止。"""
    task = SimpleNamespace(id="task", base_commit="a" * 40)
    with pytest.raises(BenchmarkError, match="checkout is missing"):
        _validate_checkout_commit(tmp_path / "missing", task)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        "tracefix.retrieval_eval.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="b" * 40),
    )
    with pytest.raises(BenchmarkError, match="does not match"):
        _validate_checkout_commit(workspace, task)


def test_metrics_average_and_empty_target_error() -> None:
    """聚合采用任务等权平均，错误工件不能被静默当作零分。"""
    one = _rank_metrics(("a.py",), ("a.py",))
    zero = _rank_metrics(("b.py",), ("a.py",))
    assert _average_metrics(iter((one, zero))).hit_at_1 == 0.5
    with pytest.raises(BenchmarkError, match="requires at least one"):
        _rank_metrics(("a.py",), ())
    with pytest.raises(BenchmarkError, match="empty retrieval"):
        _average_metrics(())
