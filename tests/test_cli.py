from types import SimpleNamespace

from tracefix.agent import AgentStatus
from tracefix.cli import main
from tracefix.context import ContextMetrics


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
