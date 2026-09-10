import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tracefix import RealIssueTask, load_real_issue_tasks
from tracefix.cli import main
from tracefix.exceptions import BenchmarkError
from tracefix.real_benchmark import _sha256

REAL_TASKS_DIR = Path(__file__).parents[1] / "benchmarks" / "real_tasks"
REAL_VALIDATION_PATH = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "experiments"
    / "v0.5.0-real-task-fixture-validation.json"
)
REAL_PRESCREEN_PATH = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "experiments"
    / "v0.5.0-real-issue-32k-prescreen.json"
)


GOLD_PATCH = """diff --git a/pkg/a.py b/pkg/a.py
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -1 +1 @@
-VALUE = 0
+VALUE = 1
diff --git a/pkg/b.py b/pkg/b.py
--- a/pkg/b.py
+++ b/pkg/b.py
@@ -1 +1 @@
-ENABLED = False
+ENABLED = True
"""

TEST_PATCH = """diff --git a/tests/test_hidden.py b/tests/test_hidden.py
new file mode 100644
--- /dev/null
+++ b/tests/test_hidden.py
@@ -0,0 +1,4 @@
+from pkg.a import VALUE
+from pkg.b import ENABLED
+
+assert VALUE == 1 and ENABLED
"""


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _digest(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_task(tmp_path: Path) -> tuple[Path, Path, str]:
    upstream = tmp_path / "upstream"
    (upstream / "pkg").mkdir(parents=True)
    (upstream / "tests").mkdir()
    (upstream / "pkg" / "a.py").write_text("VALUE = 0\n", encoding="utf-8")
    (upstream / "pkg" / "b.py").write_text("ENABLED = False\n", encoding="utf-8")
    for name in ("c.py", "d.py", "e.py"):
        (upstream / "pkg" / name).write_text("# related context\n", encoding="utf-8")
    _git(upstream, "init", "--quiet")
    _git(upstream, "add", "--all")
    _git(
        upstream,
        "-c",
        "user.name=TraceFix Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "base",
    )
    commit = _git(upstream, "rev-parse", "HEAD")

    task_dir = tmp_path / "tasks" / "owner__repo-1"
    task_dir.mkdir(parents=True)
    (task_dir / "problem.md").write_text("A real issue description.\n", encoding="utf-8")
    (task_dir / "gold.patch").write_text(GOLD_PATCH, encoding="utf-8")
    (task_dir / "test.patch").write_text(TEST_PATCH, encoding="utf-8")
    payload = {
        "id": "owner__repo-1",
        "title": "Example real issue",
        "repo": "owner/repo",
        "repo_url": "https://github.com/owner/repo.git",
        "issue_url": "https://github.com/owner/repo/issues/1",
        "base_commit": commit,
        "environment_setup_commit": commit,
        "upstream_version": "1.0",
        "issue_created_at": "2024-01-01T00:00:00Z",
        "problem_statement_kind": "curated_excerpt",
        "fail_to_pass": ["tests/test_hidden.py"],
        "test_command": "pytest -q tests/test_hidden.py",
        "pass_to_pass_count": 2,
        "expected_source_files": ["pkg/a.py", "pkg/b.py"],
        "expected_test_files": ["tests/test_hidden.py"],
        "related_context_files": [
            "pkg/a.py",
            "pkg/b.py",
            "pkg/c.py",
            "pkg/d.py",
            "pkg/e.py"
        ],
        "hashes": {
            "problem_statement": _digest(task_dir / "problem.md"),
            "gold_patch": _digest(task_dir / "gold.patch"),
            "test_patch": _digest(task_dir / "test.patch"),
        },
    }
    (task_dir / "task.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    return task_dir, upstream, commit


def test_real_task_load_validates_artifacts_and_public_problem(tmp_path) -> None:
    task_dir, _, _ = _make_task(tmp_path)
    task = RealIssueTask.load(task_dir)
    validation = task.validate_artifacts()

    assert task.problem_statement == "A real issue description.\n"
    assert validation.artifacts_valid is True
    assert validation.source_files == ("pkg/a.py", "pkg/b.py")
    assert validation.test_files == ("tests/test_hidden.py",)


def test_real_task_rejects_artifact_drift(tmp_path) -> None:
    task_dir, _, _ = _make_task(tmp_path)
    (task_dir / "problem.md").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(BenchmarkError, match="checksum mismatch"):
        RealIssueTask.load(task_dir)


def test_real_task_hash_is_stable_across_lf_and_crlf(tmp_path) -> None:
    """Git 自动换行转换不能让同一个任务在 Windows CI 中变成另一道题。"""
    lf = tmp_path / "lf.patch"
    crlf = tmp_path / "crlf.patch"
    lf.write_bytes(b"line one\nline two\n")
    crlf.write_bytes(b"line one\r\nline two\r\n")

    assert _sha256(lf) == _sha256(crlf)


def test_real_task_checkout_is_pinned_and_combined_patches_apply(tmp_path) -> None:
    task_dir, upstream, commit = _make_task(tmp_path)
    task = RealIssueTask.load(task_dir)
    # 单元测试改用本地上游仓库；正式清单仍强制使用 canonical GitHub HTTPS URL。
    task.repo_url = str(upstream)
    checkout = task.prepare_checkout(tmp_path / "checkout")
    validation = task.validate_checkout(checkout)

    assert _git(checkout, "rev-parse", "HEAD") == commit
    assert validation.checkout_valid is True
    assert validation.combined_patch_applicable is True


def test_hidden_patch_is_applied_only_when_explicitly_requested(tmp_path) -> None:
    task_dir, upstream, _ = _make_task(tmp_path)
    task = RealIssueTask.load(task_dir)
    task.repo_url = str(upstream)
    checkout = task.prepare_checkout(tmp_path / "checkout")

    assert not (checkout / "tests" / "test_hidden.py").exists()
    task.apply_hidden_tests(checkout)
    assert (checkout / "tests" / "test_hidden.py").is_file()
    assert (checkout / "pkg" / "a.py").read_text(encoding="utf-8") == "VALUE = 0\n"


def test_real_task_loader_sorts_and_rejects_unknown_ids(tmp_path) -> None:
    task_dir, _, _ = _make_task(tmp_path)
    tasks = load_real_issue_tasks(task_dir.parent)
    assert [task.id for task in tasks] == ["owner__repo-1"]

    with pytest.raises(BenchmarkError, match="unknown real task IDs"):
        load_real_issue_tasks(task_dir.parent, task_ids=("missing",))


def test_real_task_manifest_rejects_noncanonical_issue_url(tmp_path) -> None:
    task_dir, _, _ = _make_task(tmp_path)
    manifest = task_dir / "task.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["issue_url"] = "https://example.com/issues/1"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BenchmarkError, match="issue_url"):
        RealIssueTask.load(task_dir)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("repo_url", "https://example.com/repo.git", "repo_url"),
        ("expected_source_files", ["pkg/a.py", "pkg/a.py"], "duplicate"),
        ("expected_test_files", ["pkg/a.py"], "overlap"),
        (
            "related_context_files",
            ["pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/d.py", "pkg/d.py"],
            "duplicate",
        ),
        (
            "related_context_files",
            ["pkg/a.py", "pkg/c.py", "pkg/d.py", "pkg/e.py", "pkg/f.py"],
            "include every gold source",
        ),
        ("test_command", "tox -e py", "must use pytest"),
        ("problem_statement_file", "../problem.md", "safe repository-relative"),
    ],
)
def test_real_task_manifest_rejects_unsafe_or_inconsistent_fields(
    tmp_path, field, value, error
) -> None:
    """任务身份、路径和文件集合约束必须在读取工件前失败。"""
    task_dir, _, _ = _make_task(tmp_path)
    manifest = task_dir / "task.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload[field] = value
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BenchmarkError, match=error):
        RealIssueTask.load(task_dir)


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("not a patch\n", "no git diff headers"),
        ("diff --git a/pkg/a.py b/pkg/b.py\n", "renames are not supported"),
        ("diff --git a/../a.py b/../a.py\n", "safe repository-relative"),
    ],
)
def test_patch_path_parser_rejects_invalid_headers(tmp_path, content, error) -> None:
    """隐藏验收与 gold patch 不能借路径或改名逃出固定任务边界。"""
    patch = tmp_path / "invalid.patch"
    patch.write_text(content, encoding="utf-8")

    with pytest.raises(BenchmarkError, match=error):
        RealIssueTask._patch_paths(patch)


def test_real_task_loader_rejects_missing_and_empty_directories(tmp_path) -> None:
    """任务根目录必须存在且至少含一份清单。"""
    with pytest.raises(BenchmarkError, match="does not exist"):
        load_real_issue_tasks(tmp_path / "missing")

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BenchmarkError, match="no real issue tasks"):
        load_real_issue_tasks(empty)


def test_prepare_checkout_refuses_existing_destination(tmp_path) -> None:
    """真实任务检出不得覆盖已有目录。"""
    task_dir, _, _ = _make_task(tmp_path)
    task = RealIssueTask.load(task_dir)
    destination = tmp_path / "existing"
    destination.mkdir()

    with pytest.raises(BenchmarkError, match="already exists"):
        task.prepare_checkout(destination)


def test_checked_in_real_tasks_are_multifile_and_hash_pinned() -> None:
    tasks = load_real_issue_tasks(REAL_TASKS_DIR)

    assert [task.id for task in tasks] == [
        "pylint-dev__pylint-8898",
        "pytest-dev__pytest-8399",
        "sphinx-doc__sphinx-9461",
    ]
    assert all(len(task.expected_source_files) >= 2 for task in tasks)
    assert all(task.problem_statement_kind == "curated_excerpt" for task in tasks)
    assert len({task.repo for task in tasks}) == 3


def test_checked_in_real_task_validation_has_honest_boundaries() -> None:
    payload = json.loads(REAL_VALIDATION_PATH.read_text(encoding="utf-8"))
    serialized = json.dumps(payload).casefold()

    assert len(payload["tasks"]) == 3
    assert all(task["checkout_valid"] for task in payload["tasks"])
    assert all(task["combined_patch_applicable"] for task in payload["tasks"])
    assert payload["validation"]["tests_executed"] is True
    assert payload["validation"]["all_initial_hidden_tests_failed"] is True
    assert payload["validation"]["all_gold_hidden_tests_passed"] is True
    assert all(task["test_environment_fingerprint"] for task in payload["tasks"])
    assert payload["validation"]["model_runs"] == 0
    assert "d:\\tracefix" not in serialized
    assert "api_key" not in serialized


def test_checked_in_real_prescreen_is_sanitized_and_did_not_fake_ab() -> None:
    payload = json.loads(REAL_PRESCREEN_PATH.read_text(encoding="utf-8"))
    serialized = json.dumps(payload).casefold()

    assert payload["aggregate"]["task_count"] == 3
    assert payload["aggregate"]["eligible_task_count"] == 0
    assert payload["aggregate"]["history_compaction_count"] == 0
    assert payload["aggregate"]["formal_paired_experiment_started"] is False
    assert payload["decision"]["formal_paired_experiment"] == "skipped"
    assert "d:\\tracefix" not in serialized
    assert "api_key" not in serialized


def test_validate_real_tasks_cli_runs_offline(tmp_path, capsys) -> None:
    task_dir, _, _ = _make_task(tmp_path)

    exit_code = main(["validate-real-tasks", "--tasks", str(task_dir.parent)])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload[0]["task_id"] == "owner__repo-1"
    assert payload[0]["checkout_valid"] is None
