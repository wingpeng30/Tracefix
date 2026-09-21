import sys
from pathlib import Path
from types import SimpleNamespace

from tracefix.agent import AgentStatus
from tracefix.cli import main
from tracefix.context import ContextMetrics
from tracefix.exceptions import BenchmarkError


def _run_result(status: AgentStatus = AgentStatus.COMPLETED):
    return SimpleNamespace(
        status=status,
        step_count=2,
        input_tokens=10,
        output_tokens=4,
        cost_complete=True,
        cost_usd=0.01,
        cost_cny_estimate=0.072,
        usd_cny_rate=7.2,
        result_path="result.json",
        diff_path="patch.diff",
        final_output="done",
        error=None,
        context_metrics=ContextMetrics(),
    )


def test_cli_p2_summarize_is_read_only(tmp_path, monkeypatch, capsys) -> None:
    expected = tmp_path / "p2-summary.json"
    monkeypatch.setattr("tracefix.cli.write_p2_summary", lambda root: expected)
    assert main(["p2-summarize", "--experiment-dir", str(tmp_path)]) == 0
    assert str(expected) in capsys.readouterr().out


def test_cli_p2_diagnose_is_read_only(tmp_path, monkeypatch, capsys) -> None:
    expected = tmp_path / "p2-diagnostic.json"
    monkeypatch.setattr("tracefix.cli.write_p2_diagnostic", lambda root, output_dir=None: expected)
    assert main(["p2-diagnose", "--experiment-dir", str(tmp_path)]) == 0
    assert str(expected) in capsys.readouterr().out


def test_cli_p2_formal_freezes_cny_cache_pricing(tmp_path, monkeypatch, capsys) -> None:
    """正式入口必须把人民币缓存价格和上限原样交给冻结协议。"""
    captured = []
    summary = SimpleNamespace(
        completed_count=0, trial_count=60, resumed_count=0, summary_path="p2.json"
    )
    monkeypatch.setattr(
        "tracefix.cli.run_p2_formal",
        lambda config, *, experiment_dir: captured.append((config, experiment_dir)) or summary,
    )

    assert (
        main(
            [
                "p2-run",
                "--mode",
                "formal",
                "--experiment-dir",
                str(tmp_path / "formal"),
                "--source-root",
                str(tmp_path / "sources"),
                "--model-name",
                "deepseek/deepseek-flash",
                "--provider",
                "DeepSeek official direct",
                "--pricing-source",
                "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                "--currency",
                "CNY",
                "--total-cost-cap-cny",
                "100",
                "--input-cache-hit-cost-per-million",
                "0.04",
                "--input-cache-miss-cost-per-million",
                "2",
                "--output-cost-per-million",
                "8",
            ]
        )
        == 0
    )
    config, root = captured[0]
    assert root == tmp_path / "formal"
    assert config.formal.currency == "CNY"
    assert config.formal.total_cost_cap_usd == 100
    assert config.formal.conservative_input_price == 2
    assert "P2 正式实验: 0/60" in capsys.readouterr().out


def test_cli_p2_reconcile_passes_bill_filter(tmp_path, monkeypatch) -> None:
    captured = []
    expected = tmp_path / "reconciliation.json"
    monkeypatch.setattr(
        "tracefix.cli.write_p2_reconciliation",
        lambda root, *, bill_path, api_key_name: (
            captured.append((root, bill_path, api_key_name)) or expected
        ),
    )
    bill = tmp_path / "bill.csv"
    assert (
        main(
            [
                "p2-reconcile",
                "--experiment-dir",
                str(tmp_path / "old"),
                "--bill",
                str(bill),
                "--api-key-name",
                "Tracefix",
            ]
        )
        == 0
    )
    assert captured == [(tmp_path / "old", bill, "Tracefix")]


def test_cli_run_reads_task_file_and_cli_values_override_environment(
    tmp_path, monkeypatch, capsys
) -> None:
    task_file = tmp_path / "issue.md"
    task_file.write_text("修复问题", encoding="utf-8")
    captured = []

    class FakeRunner:
        def run(self, config):
            captured.append(config)
            return _run_result()

    monkeypatch.setattr("tracefix.cli.TraceFixRunner", FakeRunner)
    monkeypatch.setenv("TRACEFIX_MODEL", "env/model")
    monkeypatch.setenv("TRACEFIX_USD_CNY_RATE", "6.8")

    code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--task-file",
            str(task_file),
            "--model",
            "cli/model",
            "--usd-cny-rate",
            "7.3",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )

    assert code == 0
    assert captured[0].task == "修复问题"
    assert captured[0].model_name == "cli/model"
    assert captured[0].usd_cny_rate == 7.3
    assert "状态: completed" in capsys.readouterr().out


def test_cli_eval_uses_environment_defaults_and_returns_nonzero_for_unresolved(
    tmp_path, monkeypatch, capsys
) -> None:
    captured = []

    class FakeBenchmarkRunner:
        def run(self, config):
            captured.append(config)
            return SimpleNamespace(
                resolved_count=0,
                task_count=1,
                resolved_rate=0.0,
                summary_path="summary.json",
            )

    monkeypatch.setattr("tracefix.cli.BenchmarkRunner", FakeBenchmarkRunner)
    monkeypatch.setenv("TRACEFIX_MODEL", "env/model")
    monkeypatch.setenv("TRACEFIX_MAX_STEPS", "12")

    code = main(
        [
            "eval",
            "--tasks",
            str(tmp_path),
            "--limit",
            "1",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )

    assert code == 1
    assert captured[0].model_name == "env/model"
    assert captured[0].agent_config.max_steps == 12
    assert "0/1" in capsys.readouterr().out


def test_real_prescreen_uses_larger_default_input_budget(tmp_path, monkeypatch, capsys) -> None:
    """真实 Issue 默认 350k，普通 eval 的 80k 默认值不能被意外改变。"""
    captured = []

    class FakePrescreenRunner:
        def run(self, config):
            captured.append(config)
            return SimpleNamespace(
                eligible_task_ids=(), results=(), summary_path="real-summary.json"
            )

    monkeypatch.setattr("tracefix.cli.RealPrescreenRunner", FakePrescreenRunner)
    code = main(
        [
            "real-prescreen",
            "--tasks",
            str(tmp_path),
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )

    assert code == 0
    assert captured[0].agent_config.max_input_tokens == 350_000
    assert "真实任务预筛选完成" in capsys.readouterr().out


def test_cli_paired_eval_builds_three_repeat_experiment(tmp_path, monkeypatch, capsys) -> None:
    captured = []

    class FakePairedRunner:
        def run(self, config):
            captured.append(config)
            return SimpleNamespace(
                control=SimpleNamespace(resolved_count=3, trial_count=3),
                treatment=SimpleNamespace(resolved_count=2, trial_count=3),
                summary_path="paired-summary.json",
            )

    monkeypatch.setattr("tracefix.cli.PairedExperimentRunner", FakePairedRunner)
    code = main(
        [
            "paired-eval",
            "--tasks",
            str(tmp_path),
            "--repetitions",
            "3",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )
    assert code == 0
    assert captured[0].repetitions == 3
    assert captured[0].trigger_tokens == 32_000
    assert "关闭压缩 3/3" in capsys.readouterr().out


def test_cli_reports_invalid_numeric_environment(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("TRACEFIX_USD_CNY_RATE", "not-a-number")

    code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--task",
            "fix",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )

    assert code == 2
    assert "TRACEFIX_USD_CNY_RATE" in capsys.readouterr().err


def test_cli_does_not_silently_replace_out_of_range_environment_value(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("TRACEFIX_MAX_STEPS", "0")

    code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--task",
            "fix",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )

    assert code == 2
    assert "max_steps" in capsys.readouterr().err


def test_cli_context_options_override_environment(tmp_path, monkeypatch) -> None:
    """CLI 显式值覆盖环境变量，关闭开关可以构造未压缩对照组。"""
    captured = []

    class FakeRunner:
        def run(self, config):
            captured.append(config)
            return _run_result()

    monkeypatch.setattr("tracefix.cli.TraceFixRunner", FakeRunner)
    monkeypatch.setenv("TRACEFIX_CONTEXT_TRIGGER_TOKENS", "64000")
    code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--task",
            "fix",
            "--no-context-compaction",
            "--context-window-tokens",
            "1000000",
            "--context-trigger-tokens",
            "3000",
            "--context-retain-ratio",
            "0.4",
            "--record-request-views",
            "--max-exploration-steps",
            "3",
            "--max-search-calls",
            "2",
            "--max-file-reads-before-patch",
            "6",
            "--repo-map-reads-before-patch",
            "1",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )
    assert code == 0
    context = captured[0].agent_config.context
    assert context.enabled is False
    assert context.context_window_tokens == 1_000_000
    assert context.compaction_trigger_tokens == 3_000
    assert context.retain_ratio == 0.4
    assert captured[0].agent_config.record_request_views is True
    assert captured[0].agent_config.max_exploration_steps == 3
    assert captured[0].agent_config.max_search_calls == 2
    assert captured[0].agent_config.max_file_reads_before_patch == 6
    assert captured[0].agent_config.repo_map_reads_before_patch == 1


def test_cli_token_optimization_switch_overrides_environment(tmp_path, monkeypatch) -> None:
    """CLI 显式关闭应优先于环境变量，便于构造公平的 Token 对照组。"""
    captured = []

    class FakeRunner:
        def run(self, config):
            captured.append(config)
            return _run_result()

    monkeypatch.setattr("tracefix.cli.TraceFixRunner", FakeRunner)
    monkeypatch.setenv("TRACEFIX_TOKEN_OPTIMIZATION_ENABLED", "true")
    assert (
        main(
            [
                "run",
                "--repo",
                str(tmp_path),
                "--task",
                "fix",
                "--no-token-optimization",
                "--env-file",
                str(tmp_path / "missing.env"),
            ]
        )
        == 0
    )
    assert captured[0].agent_config.token_optimization_enabled is False


def test_cli_repo_map_options_override_environment(tmp_path, monkeypatch) -> None:
    captured = []

    class FakeRunner:
        def run(self, config):
            captured.append(config)
            return _run_result()

    monkeypatch.setattr("tracefix.cli.TraceFixRunner", FakeRunner)
    monkeypatch.setenv("TRACEFIX_REPO_MAP_ENABLED", "false")
    assert (
        main(
            [
                "run",
                "--repo",
                str(tmp_path),
                "--task",
                "fix",
                "--repo-map",
                "--repo-map-max-chars",
                "2048",
                "--env-file",
                str(tmp_path / "none"),
            ]
        )
        == 0
    )
    assert captured[0].agent_config.repo_map.enabled is True
    assert captured[0].agent_config.repo_map.max_chars == 2048


def test_cli_rejects_invalid_context_boolean_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRACEFIX_CONTEXT_ENABLED", "sometimes")
    assert (
        main(
            [
                "run",
                "--repo",
                str(tmp_path),
                "--task",
                "fix",
                "--env-file",
                str(tmp_path / "missing.env"),
            ]
        )
        == 2
    )


def test_cli_runs_offline_retrieval_evaluation(tmp_path, monkeypatch, capsys) -> None:
    """该命令不解析共享 LLM 配置，也不应要求任何 API Key。"""
    captured = []

    class FakeEvaluator:
        def run(self, config):
            captured.append(config)
            return SimpleNamespace(
                task_count=2,
                baseline_metrics=SimpleNamespace(hit_at_5=0.5),
                repo_map_metrics=SimpleNamespace(hit_at_5=1.0),
                summary_path="retrieval-summary.json",
            )

    monkeypatch.setattr("tracefix.cli.RetrievalEvaluator", FakeEvaluator)
    code = main(
        [
            "retrieval-eval",
            "--tasks",
            str(tmp_path / "tasks"),
            "--source-root",
            str(tmp_path / "sources"),
            "--output-dir",
            str(tmp_path / "outputs"),
            "--repo-map-max-chars",
            "2048",
        ]
    )

    assert code == 0
    assert captured[0].repo_map.max_chars == 2048
    assert "不调用 LLM" in capsys.readouterr().out


def test_cli_direct_task_reports_interruption_and_incomplete_cost(
    tmp_path, monkeypatch, capsys
) -> None:
    class FakeRunner:
        def run(self, config):
            result = _run_result(AgentStatus.INTERRUPTED)
            result.cost_complete = False
            result.final_output = None
            return result

    monkeypatch.setattr("tracefix.cli.TraceFixRunner", FakeRunner)
    code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--task",
            "fix",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )

    assert code == 2
    assert "费用数据不完整" in capsys.readouterr().out


def test_cli_prints_structured_run_error(tmp_path, monkeypatch, capsys) -> None:
    class FakeRunner:
        def run(self, config):
            result = _run_result(AgentStatus.FAILED)
            result.error = {
                "code": "run_configuration_error",
                "message": "DEEPSEEK_API_KEY is required for DeepSeek models",
            }
            return result

    monkeypatch.setattr("tracefix.cli.TraceFixRunner", FakeRunner)
    code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--task",
            "fix",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )

    assert code == 1
    assert "DEEPSEEK_API_KEY" in capsys.readouterr().err


def test_cli_reports_unreadable_task_file(tmp_path, capsys) -> None:
    code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--task-file",
            str(tmp_path / "missing.md"),
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )
    assert code == 2
    assert "无法读取任务文件" in capsys.readouterr().err


def test_cli_real_environment_management_commands(tmp_path, monkeypatch, capsys) -> None:
    """真实环境准备、盘点和预览清理应保留稳定的 CLI 数据流。"""
    captured = []

    class FakePreparer:
        def prepare(self, config):
            captured.append(config)
            return SimpleNamespace(
                results=(SimpleNamespace(status="ready"), SimpleNamespace(status="install_failed")),
                summary_path="environment-preparation.json",
            )

    class Dumpable(SimpleNamespace):
        def model_dump(self, **_kwargs):
            return vars(self)

    monkeypatch.setattr("tracefix.cli.RealEnvironmentPreparer", FakePreparer)
    monkeypatch.setattr(
        "tracefix.cli.inspect_storage",
        lambda _root: Dumpable(root="envs", free_bytes=1),
    )
    monkeypatch.setattr(
        "tracefix.cli.discover_interpreters",
        lambda _root: (Dumpable(executable="python", version="3.11", source="fixture"),),
    )
    monkeypatch.setattr(
        "tracefix.cli.preview_cleanup",
        lambda _root: Dumpable(paths=("managed",), bytes_reclaimable=10),
    )

    assert main(["prepare-real-environments", "--tasks", str(tmp_path)]) == 0
    assert captured[0].allow_create_interpreter is True
    assert main(["inspect-real-environments", "--environment-root", str(tmp_path)]) == 0
    assert main(["clean-real-artifacts", "--environment-root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "1/2 可用" in output
    assert '"applied": false' in output


def test_cli_validate_real_behavior_persists_success_and_task_error(
    tmp_path, monkeypatch, capsys
) -> None:
    """逐题行为验收应在单题失败后继续，并持续写入部分结果。"""
    tasks = (
        SimpleNamespace(id="owner__repo-1"),
        SimpleNamespace(id="owner__repo-2"),
    )
    monkeypatch.setattr("tracefix.cli.load_real_issue_tasks", lambda *_args, **_kwargs: tasks)
    monkeypatch.setattr("tracefix.cli.load_environment_recipes", lambda _path: {})
    monkeypatch.setattr("tracefix.cli._real_task_python", lambda *_args: Path(__file__))

    class Validation:
        def model_dump(self, **_kwargs):
            return {"task_id": "owner__repo-1", "eligible_for_llm_prescreen": True}

    calls = []

    def validate(task, **_kwargs):
        calls.append(task.id)
        if task.id.endswith("2"):
            raise BenchmarkError("fixture failure")
        return Validation()

    monkeypatch.setattr("tracefix.cli.validate_real_task_behavior", validate)
    output = tmp_path / "behavior"
    assert (
        main(
            [
                "validate-real-behavior",
                "--tasks",
                str(tmp_path),
                "--source-root",
                str(tmp_path),
                "--test-env-root",
                str(tmp_path),
                "--output-dir",
                str(output),
            ]
        )
        == 0
    )
    payload = (output / "behavior-validation.json").read_text(encoding="utf-8")
    assert calls == ["owner__repo-1", "owner__repo-2"]
    assert "fixture failure" in payload
    assert "验收记录" in capsys.readouterr().err


def test_cli_validate_real_tasks_and_candidate_commands(tmp_path, monkeypatch, capsys) -> None:
    """真实任务清单校验、候选收集和结构筛选均应传递显式配置。"""

    class Dumpable(SimpleNamespace):
        def model_dump(self, **_kwargs):
            return vars(self)

    task = SimpleNamespace(
        id="owner__repo-1",
        validate_artifacts=lambda: Dumpable(task_id="owner__repo-1", valid=True),
    )
    monkeypatch.setattr("tracefix.cli.load_real_issue_tasks", lambda *_args, **_kwargs: (task,))
    collected = []
    monkeypatch.setattr(
        "tracefix.cli.collect_candidates",
        lambda config: (
            collected.append(config) or SimpleNamespace(selected=(1, 2), output_path="pool.json")
        ),
    )

    class FakeEvaluator:
        def run(self, config):
            collected.append(config)
            return SimpleNamespace(
                task_count=1,
                repo_map_metrics=SimpleNamespace(hit_at_5=1.0),
                summary_path="screen.json",
            )

    monkeypatch.setattr("tracefix.cli.RetrievalEvaluator", FakeEvaluator)
    assert main(["validate-real-tasks", "--tasks", str(tmp_path)]) == 0
    assert (
        main(
            [
                "collect-real-candidates",
                "--source",
                str(tmp_path / "source.json"),
                "--output-dir",
                str(tmp_path / "out"),
                "--per-repository",
                "1",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "screen-real-candidates",
                "--tasks",
                str(tmp_path),
                "--source-root",
                str(tmp_path),
                "--output-dir",
                str(tmp_path / "screen"),
                "--task-id",
                "owner__repo-1",
            ]
        )
        == 0
    )
    assert len(collected) == 2
    output = capsys.readouterr().out
    assert "候选池生成完成: 2" in output
    assert "候选结构筛选完成: 1" in output


def test_cli_validate_real_tasks_prepares_missing_checkout(tmp_path, monkeypatch) -> None:
    """显式 checkout 校验应创建缺失副本，并以检出结果进行验证。"""
    calls = []

    class Dumpable(SimpleNamespace):
        def model_dump(self, **_kwargs):
            return vars(self)

    task = SimpleNamespace(id="owner__repo-1")
    task.validate_artifacts = lambda: Dumpable(valid=True)
    task.prepare_checkout = lambda path: calls.append(("prepare", path)) or path
    task.validate_checkout = lambda path: calls.append(("validate", path)) or Dumpable(valid=True)
    monkeypatch.setattr("tracefix.cli.load_real_issue_tasks", lambda *_args, **_kwargs: (task,))
    checkout_root = tmp_path / "checkouts"
    assert (
        main(
            [
                "validate-real-tasks",
                "--tasks",
                str(tmp_path),
                "--with-checkout",
                "--checkout-dir",
                str(checkout_root),
            ]
        )
        == 0
    )
    expected = (checkout_root / task.id).resolve()
    assert calls == [("prepare", expected), ("validate", expected)]


def test_cli_behavior_rejects_unsupported_and_incompatible_recipe(tmp_path, monkeypatch) -> None:
    """平台与 Python 不兼容必须逐题落盘，不能开始行为验收。"""
    tasks = (SimpleNamespace(id="unsupported"), SimpleNamespace(id="incompatible"))

    class Recipe:
        def __init__(self, supported):
            self.supported = supported

        def supports_current_platform(self):
            return self.supported

        def supports_python(self, _version):
            return False

    monkeypatch.setattr("tracefix.cli.load_real_issue_tasks", lambda *_args, **_kwargs: tasks)
    monkeypatch.setattr(
        "tracefix.cli.load_environment_recipes",
        lambda _path: {"unsupported": Recipe(False), "incompatible": Recipe(True)},
    )
    output = tmp_path / "behavior-errors"
    assert (
        main(
            [
                "validate-real-behavior",
                "--tasks",
                str(tmp_path),
                "--source-root",
                str(tmp_path),
                "--output-dir",
                str(output),
                "--test-python",
                sys.executable,
            ]
        )
        == 0
    )
    payload = (output / "behavior-validation.json").read_text(encoding="utf-8")
    assert "platform is unsupported" in payload
    assert "incompatible with task recipe" in payload


def test_cli_real_repo_map_prescreen_runs_both_arms(tmp_path, monkeypatch, capsys) -> None:
    """Repo Map 预筛选命令应构造真实实验配置并报告两组结果。"""
    captured = []

    class FakeRunner:
        def run(self, config):
            captured.append(config)
            aggregate = SimpleNamespace(resolved_count=1, trial_count=2)
            return SimpleNamespace(
                control=aggregate,
                treatment=aggregate,
                summary_path="repo-map-summary.json",
            )

    monkeypatch.setattr("tracefix.cli.RealRepoMapPrescreenRunner", FakeRunner)
    code = main(
        [
            "real-repo-map-prescreen",
            "--tasks",
            str(tmp_path),
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )
    assert code == 0
    assert captured[0].agent_config.max_input_tokens == 350_000
    assert "关闭 1/2；开启 1/2" in capsys.readouterr().out

    assert (
        main(
            [
                "real-repo-map-prescreen",
                "--tasks",
                str(tmp_path),
                "--repo-map",
                "--env-file",
                str(tmp_path / "missing.env"),
            ]
        )
        == 2
    )
