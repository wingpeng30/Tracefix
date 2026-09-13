import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tracefix import AgentConfig, AgentStatus, LLMConfig, RunResult
from tracefix.exceptions import BenchmarkError
from tracefix.paired import ExperimentArm
from tracefix.provenance import collect_run_provenance
from tracefix.real_benchmark import RealIssueTask
from tracefix.real_experiment import (
    RealExperimentConfig,
    RealPairedExperimentRunner,
    RealPrescreenRunner,
    RealRepoMapPrescreenRunner,
    RealTrajectoryMetrics,
    RealTrialResult,
    _eligibility_failures,
    _git,
    _task_python,
    analyze_real_trajectory,
    validate_real_task_behavior,
)

GOLD = """diff --git a/pkg/a.py b/pkg/a.py
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

HIDDEN = """diff --git a/tests/test_hidden.py b/tests/test_hidden.py
new file mode 100644
--- /dev/null
+++ b/tests/test_hidden.py
@@ -0,0 +1,5 @@
+from pkg.a import VALUE
+from pkg.b import ENABLED
+
+def test_fixed():
+    assert VALUE == 1 and ENABLED
"""


def test_real_experiment_default_input_budget_is_350k() -> None:
    """程序化真实实验与 CLI 的 350k 默认值必须一致。"""
    assert RealExperimentConfig().agent_config.max_input_tokens == 350_000


def _run_git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _digest(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fixture(tmp_path: Path) -> tuple[RealIssueTask, Path]:
    source = tmp_path / "source"
    (source / "pkg").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "pkg" / "a.py").write_text("VALUE = 0\n", encoding="utf-8")
    (source / "pkg" / "b.py").write_text("ENABLED = False\n", encoding="utf-8")
    for name in ("c.py", "d.py", "e.py"):
        (source / "pkg" / name).write_text("# context\n", encoding="utf-8")
    _run_git(source, "init", "--quiet")
    _run_git(source, "add", "--all")
    _run_git(
        source,
        "-c",
        "user.name=TraceFix Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "base",
    )
    commit = _run_git(source, "rev-parse", "HEAD")
    task_dir = tmp_path / "tasks" / "owner__repo-1"
    task_dir.mkdir(parents=True)
    (task_dir / "problem.md").write_text("Fix both flags.\n", encoding="utf-8")
    (task_dir / "gold.patch").write_text(GOLD, encoding="utf-8")
    (task_dir / "test.patch").write_text(HIDDEN, encoding="utf-8")
    manifest = {
        "id": "owner__repo-1",
        "title": "Two-file issue",
        "repo": "owner/repo",
        "repo_url": "https://github.com/owner/repo.git",
        "issue_url": "https://github.com/owner/repo/issues/1",
        "base_commit": commit,
        "environment_setup_commit": commit,
        "upstream_version": "1.0",
        "issue_created_at": "2024-01-01T00:00:00Z",
        "problem_statement_kind": "curated_excerpt",
        "fail_to_pass": ["tests/test_hidden.py"],
        "test_command": "pytest -q -p no:cacheprovider tests/test_hidden.py",
        "pass_to_pass_count": 0,
        "expected_source_files": ["pkg/a.py", "pkg/b.py"],
        "expected_test_files": ["tests/test_hidden.py"],
        "related_context_files": [
            "pkg/a.py",
            "pkg/b.py",
            "pkg/c.py",
            "pkg/d.py",
            "pkg/e.py",
        ],
        "hashes": {
            "problem_statement": _digest(task_dir / "problem.md"),
            "gold_patch": _digest(task_dir / "gold.patch"),
            "test_patch": _digest(task_dir / "test.patch"),
        },
    }
    (task_dir / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    return RealIssueTask.load(task_dir), source


def _trace(path: Path, *, eligible: bool = True) -> None:
    events = [
        {
            "event_type": "context_prepared",
            "step": 2,
            "payload": {
                "estimated_tokens_before": 33_000 if eligible else 2_000,
                "estimated_tokens_after": 12_000 if eligible else 2_000,
            },
        },
        {
            "event_type": "context_compacted",
            "step": 2,
            "payload": {"batches_compacted": 2 if eligible else 0},
        },
        {"event_type": "model_request_view", "step": 2, "payload": {}},
    ]
    for name in ("a.py", "b.py", "c.py", "d.py", "e.py"):
        events.append(
            {
                "event_type": "tool_called",
                "step": 1,
                "payload": {
                    "call": {"name": "read_file", "arguments": {"path": f"pkg/{name}"}}
                },
            }
        )
    events.extend(
        [
            {
                "event_type": "tool_called",
                "step": 2,
                "payload": {"call": {"name": "apply_patch", "arguments": {}}},
            },
            {
                "event_type": "tool_called",
                "step": 3,
                "payload": {"call": {"name": "run_tests", "arguments": {}}},
            },
            {
                "event_type": "tool_returned",
                "step": 3,
                "payload": {"result": {"tool_name": "run_tests", "success": True}},
            },
        ]
    )
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


class _FakeRunner:
    def run(self, config) -> RunResult:
        run_dir = config.output_dir / "fake-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        trace = run_dir / "trajectory.jsonl"
        diff = run_dir / "patch.diff"
        result_path = run_dir / "result.json"
        _trace(trace)
        diff.write_text(GOLD, encoding="utf-8")
        now = datetime.now(UTC)
        return RunResult(
            run_id="fake-run",
            source_repo=str(config.repo),
            source_commit=_run_git(config.repo, "rev-parse", "HEAD"),
            workspace=str(run_dir / "workspace"),
            model_name=config.model_name,
            status=AgentStatus.COMPLETED,
            stop_reason="assistant_final",
            final_output="done",
            step_count=3,
            input_tokens=100,
            output_tokens=20,
            test_runs=1,
            cost_usd=0.01,
            cost_complete=True,
            usd_cny_rate=config.usd_cny_rate,
            cost_cny_estimate=0.072,
            started_at=now,
            finished_at=now,
            duration_seconds=1,
            changed_files=("pkg/a.py", "pkg/b.py"),
            trace_path=str(trace),
            diff_path=str(diff),
            result_path=str(result_path),
            agent_config=config.agent_config,
            provenance=collect_run_provenance(
                config.task, LLMConfig(model_name=config.model_name)
            ),
        )


def test_behavior_validation_proves_initial_fail_and_gold_pass(tmp_path) -> None:
    task, source = _fixture(tmp_path)
    result = validate_real_task_behavior(
        task,
        source=source,
        test_python=Path(sys.executable),
        output_dir=tmp_path / "behavior",
    )

    assert result.initial_hidden_failed is True
    assert result.gold_hidden_passed is True
    assert result.initial_returncode != 0
    assert result.gold_returncode == 0
    assert result.test_environment.python_version


def test_prescreen_runs_hidden_verification_and_applies_entry_gate(
    tmp_path, monkeypatch
) -> None:
    task, source = _fixture(tmp_path)
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source.rename(source_root / task.id)
    monkeypatch.setattr(
        "tracefix.real_experiment._task_python", lambda *_: Path(sys.executable)
    )

    summary = RealPrescreenRunner(_FakeRunner()).run(
        RealExperimentConfig(
            tasks_dir=task.task_dir.parent,
            source_root=source_root,
            test_env_root=tmp_path / "envs",
            output_dir=tmp_path / "runs",
            task_ids=(task.id,),
        )
    )

    trial = summary.results[0]
    assert trial.agent_completed is True
    assert trial.agent_selected_tests_passed is True
    assert trial.public_tests_passed is None
    assert trial.independent_tests_passed is True
    assert trial.source_patch_applied is True
    assert trial.tests_modified is False
    assert trial.resolved is True
    assert trial.eligible_for_paired is True
    assert summary.eligible_task_ids == (task.id,)
    assert summary.eligible_for_formal_experiment is False


def test_trajectory_reports_missing_gate_reasons(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    _trace(path, eligible=False)
    metrics = analyze_real_trajectory(
        path,
        related_files=("pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/d.py", "pkg/e.py"),
    )
    failures = _eligibility_failures(metrics, 32_000)

    assert "no_history_compaction" in failures
    assert "request_never_reached_trigger" in failures
    assert metrics.request_views_recorded == 1


def test_trajectory_records_first_read_of_evaluator_only_target_file(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    _trace(path)

    metrics = analyze_real_trajectory(
        path,
        related_files=("pkg/a.py", "pkg/b.py"),
        target_files=("pkg/b.py",),
    )

    assert metrics.first_target_read_path == "pkg/b.py"
    assert metrics.first_target_read_step == 1
    assert metrics.first_target_read_tool_call == 2
    assert metrics.first_patch_step == 2
    assert metrics.first_test_step == 3


def test_gate_requires_files_loop_and_request_view() -> None:
    metrics = RealTrajectoryMetrics(max_estimated_tokens_before=32_000)
    failures = _eligibility_failures(metrics, 32_000)

    assert "fewer_than_five_meaningful_files_read" in failures
    assert "no_test_and_patch_feedback_loop" in failures
    assert "request_views_missing" in failures


def test_paired_runner_rejects_fewer_than_three_eligible_tasks(tmp_path) -> None:
    with pytest.raises(BenchmarkError, match="3 eligible"):
        RealPairedExperimentRunner().run_paired(
            RealExperimentConfig(output_dir=tmp_path),
            eligible_task_ids=("only-one",),
        )


def test_paired_runner_uses_ct_tc_ct_order_and_persists_summary(
    tmp_path, monkeypatch
) -> None:
    task, source = _fixture(tmp_path)
    config = RealExperimentConfig(output_dir=tmp_path / "runs")
    fake_run = _FakeRunner().run(
        type(
            "Config",
            (),
            {
                "output_dir": tmp_path / "fake",
                "repo": source,
                "model_name": config.model_name,
                "task": task.problem_statement,
                "usd_cny_rate": config.usd_cny_rate,
                "agent_config": AgentConfig(),
            },
        )()
    )
    tasks = tuple(task.model_copy(update={"id": f"task-{index}"}) for index in range(3))
    monkeypatch.setattr(
        "tracefix.real_experiment.load_real_issue_tasks", lambda *args, **kwargs: tasks
    )
    runner = RealPairedExperimentRunner()

    def fake_trial(config, task, arm, sequence, repetition, *paths):
        return RealTrialResult(
            sequence=sequence,
            task_id=task.id,
            arm=arm,
            repetition=repetition,
            run=fake_run,
            agent_completed=True,
            independent_tests_passed=True,
            tests_modified=False,
            resolved=True,
            trajectory=RealTrajectoryMetrics(),
            eligible_for_paired=True,
        )

    monkeypatch.setattr(runner, "_run_trial", fake_trial)
    summary = runner.run_paired(
        config,
        eligible_task_ids=tuple(item.id for item in tasks),
        repetitions=3,
    )

    assert len(summary.trials) == 18
    assert [trial.arm for trial in summary.trials[:2]] == [
        ExperimentArm.CONTROL,
        ExperimentArm.TREATMENT,
    ]
    assert [trial.arm for trial in summary.trials[6:8]] == [
        ExperimentArm.TREATMENT,
        ExperimentArm.CONTROL,
    ]
    assert Path(summary.summary_path).is_file()


def test_repo_map_prescreen_alternates_arms_and_keeps_context_policy(tmp_path, monkeypatch) -> None:
    """该预筛选的唯一变量是 Repo Map，不能意外关闭 32k 压缩。"""
    task, source = _fixture(tmp_path)
    config = RealExperimentConfig(output_dir=tmp_path / "runs")
    fake_run = _FakeRunner().run(
        type(
            "Config",
            (),
            {
                "output_dir": tmp_path / "fake",
                "repo": source,
                "model_name": config.model_name,
                "task": task.problem_statement,
                "usd_cny_rate": config.usd_cny_rate,
                "agent_config": AgentConfig(),
            },
        )()
    )
    tasks = tuple(task.model_copy(update={"id": f"task-{index}"}) for index in range(3))
    monkeypatch.setattr(
        "tracefix.real_experiment.load_real_issue_tasks", lambda *args, **kwargs: tasks
    )
    runner = RealRepoMapPrescreenRunner()
    seen = []

    def fake_trial(config, task, arm, sequence, repetition, *paths, **kwargs):
        seen.append((task.id, arm, kwargs["context_enabled"], kwargs["repo_map_enabled"]))
        return RealTrialResult(
            sequence=sequence,
            task_id=task.id,
            arm=arm,
            repo_map_enabled=kwargs["repo_map_enabled"],
            repetition=repetition,
            run=fake_run,
            agent_completed=True,
            public_tests_passed=True,
            independent_tests_passed=True,
            tests_modified=False,
            resolved=True,
            trajectory=RealTrajectoryMetrics(first_target_read_step=2),
            eligible_for_paired=True,
        )

    monkeypatch.setattr(runner, "_run_trial", fake_trial)
    summary = runner.run(config)

    assert [entry[1] for entry in seen] == [
        ExperimentArm.CONTROL,
        ExperimentArm.TREATMENT,
        ExperimentArm.TREATMENT,
        ExperimentArm.CONTROL,
        ExperimentArm.CONTROL,
        ExperimentArm.TREATMENT,
    ]
    assert all(entry[2] is config.agent_config.context.enabled for entry in seen)
    assert [entry[3] for entry in seen] == [False, True, True, False, False, True]
    assert summary.control.resolved_count == 3
    assert summary.treatment.average_first_target_read_step == 2
    assert Path(summary.summary_path).is_file()


def test_task_python_and_git_failures_are_explicit(tmp_path) -> None:
    with pytest.raises(BenchmarkError, match="interpreter"):
        _task_python(tmp_path, "missing")
    with pytest.raises(BenchmarkError, match="git command"):
        _git(["rev-parse", "--verify", "missing"], tmp_path)
