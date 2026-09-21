"""TraceFix 的非交互式命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tracefix.agent import AgentConfig, AgentStatus
from tracefix.benchmark import BenchmarkConfig, BenchmarkRunner
from tracefix.context import ContextConfig
from tracefix.exceptions import BenchmarkError, TraceFixError
from tracefix.p2_protocol import (
    P2FormalRunRequirements,
    P2ProtocolConfig,
    run_p2_formal,
    run_p2_simulation,
    write_p2_check,
    write_p2_dry_run,
    write_p2_reconciliation,
    write_p2_summary,
)
from tracefix.paired import PairedExperimentConfig, PairedExperimentRunner
from tracefix.real_benchmark import load_real_issue_tasks
from tracefix.real_candidates import CandidateCollectionConfig, collect_candidates
from tracefix.real_environment import (
    EnvironmentPreparationConfig,
    RealEnvironmentPreparer,
    apply_cleanup,
    discover_interpreters,
    inspect_storage,
    preview_cleanup,
    resolve_managed_environment_python,
)
from tracefix.real_experiment import (
    RealExperimentConfig,
    RealPairedExperimentRunner,
    RealPrescreenRunner,
    RealPrescreenSummary,
    RealRepoMapPrescreenRunner,
    validate_real_task_behavior,
)
from tracefix.real_recipes import load_environment_recipes
from tracefix.repository import RepoMapConfig
from tracefix.retrieval_eval import RetrievalEvaluationConfig, RetrievalEvaluator
from tracefix.runtime import (
    DEFAULT_MODEL_NAME,
    DEFAULT_USD_CNY_RATE,
    RunConfig,
    TraceFixRunner,
    load_environment_file,
)


def _env_number(name: str, converter: type[int] | type[float]) -> int | float | None:
    """读取数值环境变量，并给出不包含敏感值的明确错误。"""
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    try:
        return converter(value)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是有效数字") from exc


def _first(value: Any, env_name: str, default: Any) -> Any:
    """实现 CLI 参数高于环境变量、环境变量高于默认值的优先级。"""
    if value is not None:
        return value
    env_value = os.getenv(env_name)
    return env_value if env_value not in {None, ""} else default


def _env_bool(name: str, default: bool) -> bool:
    """解析常用布尔环境变量拼写，拒绝含糊值。"""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"环境变量 {name} 必须是布尔值")


def _number_or_default(
    cli_value: int | float | None,
    env_name: str,
    converter: type[int] | type[float],
    default: int | float,
) -> int | float:
    """合并数值参数，同时保留显式的 0 供 Pydantic 报告越界错误。"""
    if cli_value is not None:
        return cli_value
    env_value = _env_number(env_name, converter)
    return default if env_value is None else env_value


def _add_shared_options(parser: argparse.ArgumentParser) -> None:
    """为 run 与 eval 添加完全一致的模型、费用和预算参数。"""
    parser.add_argument("--model", help=f"LiteLLM 模型名，默认 {DEFAULT_MODEL_NAME}")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="密钥环境文件")
    parser.add_argument("--output-dir", type=Path, help="运行产物根目录，默认 runs")
    parser.add_argument("--usd-cny-rate", type=float, help="美元兑人民币估算汇率")
    parser.add_argument("--max-steps", type=int, help="最大模型请求次数")
    parser.add_argument("--max-input-tokens", type=int, help="累计输入 Token 上限")
    parser.add_argument("--max-output-tokens", type=int, help="累计输出 Token 上限")
    parser.add_argument("--wall-time-seconds", type=int, help="任务最长运行秒数")
    parser.add_argument("--max-test-runs", type=int, help="Agent 内测试调用上限")
    parser.add_argument("--max-exploration-steps", type=int, help="探索阶段模型步骤软上限")
    parser.add_argument("--max-search-calls", type=int, help="进入补丁前搜索调用软上限")
    parser.add_argument("--max-file-reads-before-patch", type=int, help="进入补丁前文件读取软上限")
    parser.add_argument(
        "--repo-map-reads-before-patch", type=int, help="触发补丁行动提示的候选文件读取数"
    )
    parser.add_argument("--llm-timeout-seconds", type=float, help="单次模型请求超时")
    parser.add_argument("--llm-max-retries", type=int, help="LiteLLM 自动重试次数")
    parser.add_argument(
        "--per-request-output-tokens",
        type=int,
        help="单次模型响应的最大输出 Token",
    )
    parser.add_argument(
        "--test-python",
        type=Path,
        help="运行被测仓库 pytest 的独立 Python 解释器",
    )
    parser.add_argument(
        "--test-pythonpath",
        type=Path,
        action="append",
        default=None,
        help="额外测试引导目录，可重复传入；仅提供给 pytest 子进程",
    )
    parser.add_argument(
        "--no-context-compaction",
        action="store_true",
        default=None,
        help="关闭工具结果裁剪和历史折叠，用于运行未压缩对照组",
    )
    parser.add_argument("--context-window-tokens", type=int, help="模型单次请求硬窗口")
    parser.add_argument("--context-trigger-tokens", type=int, help="历史折叠软阈值")
    parser.add_argument("--context-retain-ratio", type=float, help="折叠后保留近期轮次的比例")
    parser.add_argument(
        "--record-request-views",
        action="store_true",
        default=None,
        help="在轨迹中保存脱敏后的实际模型请求视图（默认关闭）",
    )
    parser.add_argument(
        "--no-token-optimization",
        action="store_true",
        default=None,
        help="关闭工具结果精简、缓存与行动引导，用作同预算对照组",
    )
    repo_map_group = parser.add_mutually_exclusive_group()
    repo_map_group.add_argument(
        "--repo-map", action="store_true", default=None, help="显式启用确定性 Python Repo Map"
    )
    repo_map_group.add_argument(
        "--no-repo-map", action="store_true", default=None, help="关闭 Repo Map，用于定位对照组"
    )
    parser.add_argument("--repo-map-max-chars", type=int, help="Repo Map 字符上限")


def build_parser() -> argparse.ArgumentParser:
    """构建 argparse 命令树，不读取环境或产生外部副作用。"""
    parser = argparse.ArgumentParser(
        prog="tracefix",
        description="在隔离 Git 克隆中运行 TraceFix Coding Agent。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="运行一个仓库修复任务")
    run_parser.add_argument("--repo", type=Path, required=True, help="干净的本地 Git 仓库")
    task_group = run_parser.add_mutually_exclusive_group(required=True)
    task_group.add_argument("--task", help="直接传入 Bug 描述")
    task_group.add_argument("--task-file", type=Path, help="从 UTF-8 文件读取 Bug 描述")
    _add_shared_options(run_parser)

    eval_parser = subparsers.add_parser("eval", help="串行运行合成基准任务")
    eval_parser.add_argument(
        "--tasks", type=Path, default=Path("benchmarks/tasks"), help="任务目录"
    )
    eval_parser.add_argument("--limit", type=int, help="只运行排序后的前 N 个任务")
    eval_parser.add_argument(
        "--task-id", action="append", default=[], help="只运行指定任务，可重复传入"
    )
    _add_shared_options(eval_parser)

    paired_parser = subparsers.add_parser(
        "paired-eval", help="交替运行关闭压缩与 32k 压缩的重复实验"
    )
    paired_parser.add_argument(
        "--tasks", type=Path, default=Path("benchmarks/context_tasks"), help="任务目录"
    )
    paired_parser.add_argument("--limit", type=int, help="只运行排序后的前 N 个任务")
    paired_parser.add_argument(
        "--task-id", action="append", default=[], help="只运行指定任务，可重复传入"
    )
    paired_parser.add_argument(
        "--repetitions", type=int, default=3, help="每题每组重复次数，至少 3"
    )
    _add_shared_options(paired_parser)

    real_parser = subparsers.add_parser(
        "validate-real-tasks", help="校验真实 GitHub Issue 任务的哈希与固定提交"
    )
    real_parser.add_argument(
        "--tasks", type=Path, default=Path("benchmarks/real_tasks"), help="真实任务目录"
    )
    real_parser.add_argument(
        "--task-id", action="append", default=[], help="只校验指定任务，可重复传入"
    )
    real_parser.add_argument(
        "--with-checkout",
        action="store_true",
        help="联网克隆上游固定提交，并执行组合补丁预检",
    )
    real_parser.add_argument(
        "--checkout-dir",
        type=Path,
        default=Path("runs/real-task-validation"),
        help="联网校验的临时检出根目录",
    )

    behavior_parser = subparsers.add_parser(
        "validate-real-behavior", help="验证真实任务原始失败且标准补丁通过"
    )
    _add_real_task_locations(behavior_parser)
    behavior_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("runs/real-task-behavior-validation"),
        help="一次性验收副本目录",
    )
    behavior_parser.add_argument("--recipes", type=Path, default=Path("benchmarks/real_recipes"))
    behavior_parser.add_argument(
        "--test-python",
        type=Path,
        help="显式复用已准备的兼容测试解释器；仍由配方校验版本，仅适合单题诊断",
    )

    environment_parser = subparsers.add_parser(
        "prepare-real-environments", help="为真实任务创建或复用独立 Python 测试环境"
    )
    _add_real_task_locations(environment_parser)
    environment_parser.add_argument(
        "--output-dir", type=Path, default=Path("runs/real-task-environment-preparation")
    )
    environment_parser.add_argument(
        "--python", type=Path, help="显式指定兼容 Python；省略时自动发现"
    )
    environment_parser.add_argument(
        "--index-url", default="https://pypi.tuna.tsinghua.edu.cn/simple"
    )
    environment_parser.add_argument("--recipes", type=Path, default=Path("benchmarks/real_recipes"))
    environment_parser.add_argument("--min-free-gib", type=int, default=10)
    environment_parser.add_argument("--no-create-interpreter", action="store_true")

    inventory_parser = subparsers.add_parser(
        "inspect-real-environments", help="盘点 TraceFix 解释器与空间"
    )
    inventory_parser.add_argument(
        "--environment-root", type=Path, default=Path("runs/real-task-envs-v2")
    )
    cleanup_parser = subparsers.add_parser(
        "clean-real-artifacts", help="预览或清理 TraceFix 登记的环境"
    )
    cleanup_parser.add_argument(
        "--environment-root", type=Path, default=Path("runs/real-task-envs-v2")
    )
    cleanup_parser.add_argument("--apply", action="store_true", help="实际删除预览中的已登记目录")

    prescreen_parser = subparsers.add_parser(
        "real-prescreen", help="真实 Issue 的单次 32k 压缩触发预筛选"
    )
    _add_real_task_locations(prescreen_parser)
    _add_shared_options(prescreen_parser)

    repo_map_prescreen_parser = subparsers.add_parser(
        "real-repo-map-prescreen",
        help="真实 Issue 上仅切换 Repo Map 的单次交替预筛选（会调用 LLM）",
    )
    _add_real_task_locations(repo_map_prescreen_parser)
    _add_shared_options(repo_map_prescreen_parser)

    real_paired_parser = subparsers.add_parser(
        "real-paired-eval", help="只对至少三道已入选真实任务执行正式配对实验"
    )
    _add_real_task_locations(real_paired_parser)
    real_paired_parser.add_argument(
        "--prescreen-summary", type=Path, required=True, help="预筛选 summary JSON"
    )
    real_paired_parser.add_argument("--repetitions", type=int, default=3)
    _add_shared_options(real_paired_parser)

    p2_parser = subparsers.add_parser(
        "p2-dry-run", help="生成 P2 整体优化 C/T 协议与零费用演练记录，不调用 LLM"
    )
    p2_parser.add_argument("--tasks", type=Path, default=Path("benchmarks/real_candidates"))
    p2_parser.add_argument("--recipes", type=Path, default=Path("benchmarks/real_recipes"))
    p2_parser.add_argument(
        "--source-root", type=Path, default=Path("runs/real-candidate-validation-v080b")
    )
    p2_parser.add_argument(
        "--test-env-root", type=Path, default=Path("runs/p1-revalidation-20260917/environments")
    )
    p2_parser.add_argument(
        "--p1-evidence",
        type=Path,
        default=Path("runs/p1-revalidation-20260917/behavior-validation/behavior-validation.json"),
    )
    p2_parser.add_argument("--output-dir", type=Path, default=Path("runs"))
    p2_run = subparsers.add_parser("p2-run", help="执行或恢复 P2 零费用工程演练")
    p2_run.add_argument("--mode", choices=("simulation", "formal"), default="simulation")
    p2_run.add_argument("--experiment-dir", type=Path, required=True)
    p2_run.add_argument("--tasks", type=Path, default=Path("benchmarks/real_candidates"))
    p2_run.add_argument("--recipes", type=Path, default=Path("benchmarks/real_recipes"))
    p2_run.add_argument("--source-root", type=Path, required=True)
    p2_run.add_argument(
        "--test-env-root",
        type=Path,
        default=Path("runs/p1-revalidation-20260917/environments"),
    )
    p2_run.add_argument(
        "--p1-evidence",
        type=Path,
        default=Path("runs/p1-revalidation-20260917/behavior-validation/behavior-validation.json"),
    )
    p2_run.add_argument("--model-name")
    p2_run.add_argument("--provider")
    p2_run.add_argument("--pricing-source")
    p2_run.add_argument("--total-cost-cap-usd", type=float)
    p2_run.add_argument("--total-cost-cap-cny", type=float)
    p2_run.add_argument("--input-cost-per-million-usd", type=float)
    p2_run.add_argument("--output-cost-per-million-usd", type=float)
    p2_run.add_argument("--currency", choices=("USD", "CNY"), default="USD")
    p2_run.add_argument("--input-cache-hit-cost-per-million", type=float)
    p2_run.add_argument("--input-cache-miss-cost-per-million", type=float)
    p2_run.add_argument("--output-cost-per-million", type=float)
    p2_run.add_argument("--prior-calculated-amount", type=float, default=0)
    p2_run.add_argument("--prior-unsettled-reservation", type=float, default=0)
    p2_run.add_argument("--campaign-ledger", type=Path)
    p2_check = subparsers.add_parser("p2-check", help="检查并冻结 P2 的源码、配方和受管环境")
    p2_check.add_argument("--tasks", type=Path, default=Path("benchmarks/real_candidates"))
    p2_check.add_argument("--recipes", type=Path, default=Path("benchmarks/real_recipes"))
    p2_check.add_argument("--source-root", type=Path, required=True)
    p2_check.add_argument(
        "--test-env-root",
        type=Path,
        default=Path("runs/p1-revalidation-20260917/environments"),
    )
    p2_check.add_argument(
        "--p1-evidence",
        type=Path,
        default=Path("runs/p1-revalidation-20260917/behavior-validation/behavior-validation.json"),
    )
    p2_check.add_argument("--output-dir", type=Path, default=Path("runs"))
    p2_summary = subparsers.add_parser("p2-summarize", help="汇总已保存的 P2 试次，不执行 Agent")
    p2_summary.add_argument("--experiment-dir", type=Path, required=True)
    p2_reconcile = subparsers.add_parser("p2-reconcile", help="只读核对 P2 账本与响应证据")
    p2_reconcile.add_argument("--experiment-dir", type=Path, required=True)
    p2_reconcile.add_argument("--bill", type=Path)
    p2_reconcile.add_argument("--api-key-name", default="Tracefix")

    retrieval_parser = subparsers.add_parser(
        "retrieval-eval", help="离线比较文件名关键词基线与 Repo Map 的文件定位能力"
    )
    retrieval_parser.add_argument(
        "--tasks", type=Path, default=Path("benchmarks/real_tasks"), help="真实任务目录"
    )
    retrieval_parser.add_argument(
        "--source-root",
        type=Path,
        default=Path("runs/real-task-validation"),
        help="按任务 ID 保存的固定源码检出目录",
    )
    retrieval_parser.add_argument(
        "--output-dir", type=Path, default=Path("runs"), help="评测产物根目录"
    )
    retrieval_parser.add_argument(
        "--task-id", action="append", default=[], help="只评测指定任务，可重复传入"
    )
    retrieval_parser.add_argument("--repo-map-max-chars", type=int, default=12_000)

    collect_parser = subparsers.add_parser(
        "collect-real-candidates", help="下载并按仓库配额生成 SWE-bench Verified 候选清单"
    )
    collect_parser.add_argument("--dataset", default="SWE-bench/SWE-bench_Verified")
    collect_parser.add_argument("--revision", default="main")
    collect_parser.add_argument(
        "--source", type=Path, help="本地 JSON/JSONL fixture；省略则使用 datasets"
    )
    collect_parser.add_argument(
        "--output-dir", type=Path, default=Path("benchmarks/real_candidates")
    )
    collect_parser.add_argument("--per-repository", type=int, default=3)
    collect_parser.add_argument(
        "--min-source-files",
        type=int,
        default=1,
        help="gold patch 至少修改的非测试 Python 文件数，默认 1",
    )
    collect_parser.add_argument("--repository", action="append", dest="repositories")

    screen_parser = subparsers.add_parser(
        "screen-real-candidates", help="对固定源码候选执行离线 Repo Map 结构筛选"
    )
    screen_parser.add_argument("--tasks", type=Path, default=Path("benchmarks/real_tasks"))
    screen_parser.add_argument(
        "--source-root", type=Path, default=Path("runs/real-task-validation")
    )
    screen_parser.add_argument("--output-dir", type=Path, default=Path("runs"))
    screen_parser.add_argument(
        "--candidate-pool", type=Path, help="collect-real-candidates 生成的清单"
    )
    screen_parser.add_argument("--task-id", action="append", default=[])
    screen_parser.add_argument("--repo-map-max-chars", type=int, default=12_000)
    return parser


def _add_real_task_locations(parser: argparse.ArgumentParser) -> None:
    """添加真实任务清单、固定源码和独立测试环境的位置参数。"""
    parser.add_argument(
        "--tasks", type=Path, default=Path("benchmarks/real_tasks"), help="真实任务目录"
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path("runs/real-task-validation"),
        help="按任务 ID 保存的固定提交源码",
    )
    parser.add_argument(
        "--test-env-root",
        type=Path,
        default=Path("runs/real-task-envs-v2"),
        help="按任务 ID 保存的独立 Python 测试环境",
    )
    parser.add_argument(
        "--task-id", action="append", default=[], help="只选择指定真实任务，可重复传入"
    )


def _resolve_shared(args: argparse.Namespace, *, real_issue_budget: bool = False) -> dict[str, Any]:
    """加载 .env 后合并 CLI、环境变量和默认值。

    真实 Issue 的一次定位、补丁和独立验收通常比合成任务长得多，因此仅真实任务
    命令使用 350k 的累计输入默认值；普通 ``run``/``eval`` 仍保留 80k，避免无意间
    扩大日常调试成本。CLI 参数和环境变量始终优先于这里的默认值。
    """
    load_environment_file(args.env_file)
    return {
        "model_name": _first(args.model, "TRACEFIX_MODEL", DEFAULT_MODEL_NAME),
        "output_dir": Path(_first(args.output_dir, "TRACEFIX_OUTPUT_DIR", "runs")),
        "env_file": args.env_file,
        "usd_cny_rate": _number_or_default(
            args.usd_cny_rate,
            "TRACEFIX_USD_CNY_RATE",
            float,
            DEFAULT_USD_CNY_RATE,
        ),
        "llm_timeout_seconds": _number_or_default(
            args.llm_timeout_seconds,
            "TRACEFIX_LLM_TIMEOUT_SECONDS",
            float,
            120.0,
        ),
        "llm_max_retries": _number_or_default(
            args.llm_max_retries,
            "TRACEFIX_LLM_MAX_RETRIES",
            int,
            2,
        ),
        "per_request_output_tokens": _number_or_default(
            args.per_request_output_tokens,
            "TRACEFIX_PER_REQUEST_OUTPUT_TOKENS",
            int,
            4_096,
        ),
        "test_python_executable": (
            Path(value)
            if (value := _first(args.test_python, "TRACEFIX_TEST_PYTHON", None))
            else None
        ),
        "test_pythonpath_entries": tuple(
            args.test_pythonpath
            if args.test_pythonpath is not None
            else (
                Path(value)
                for value in os.getenv("TRACEFIX_TEST_PYTHONPATH", "").split(os.pathsep)
                if value
            )
        ),
        "agent_config": AgentConfig(
            max_steps=_number_or_default(args.max_steps, "TRACEFIX_MAX_STEPS", int, 30),
            max_input_tokens=_number_or_default(
                args.max_input_tokens,
                "TRACEFIX_MAX_INPUT_TOKENS",
                int,
                350_000 if real_issue_budget else 80_000,
            ),
            max_output_tokens=_number_or_default(
                args.max_output_tokens, "TRACEFIX_MAX_OUTPUT_TOKENS", int, 20_000
            ),
            wall_time_seconds=_number_or_default(
                args.wall_time_seconds, "TRACEFIX_WALL_TIME_SECONDS", int, 1_200
            ),
            max_test_runs=_number_or_default(args.max_test_runs, "TRACEFIX_MAX_TEST_RUNS", int, 8),
            max_exploration_steps=_number_or_default(
                args.max_exploration_steps,
                "TRACEFIX_MAX_EXPLORATION_STEPS",
                int,
                4,
            ),
            max_search_calls=_number_or_default(
                args.max_search_calls, "TRACEFIX_MAX_SEARCH_CALLS", int, 4
            ),
            max_file_reads_before_patch=_number_or_default(
                args.max_file_reads_before_patch,
                "TRACEFIX_MAX_FILE_READS_BEFORE_PATCH",
                int,
                8,
            ),
            repo_map_reads_before_patch=_number_or_default(
                args.repo_map_reads_before_patch,
                "TRACEFIX_REPO_MAP_READS_BEFORE_PATCH",
                int,
                2,
            ),
            token_optimization_enabled=(
                not args.no_token_optimization
                if args.no_token_optimization is not None
                else _env_bool("TRACEFIX_TOKEN_OPTIMIZATION_ENABLED", True)
            ),
            record_request_views=(
                args.record_request_views
                if args.record_request_views is not None
                else _env_bool("TRACEFIX_RECORD_REQUEST_VIEWS", False)
            ),
            context=ContextConfig(
                enabled=(
                    not args.no_context_compaction
                    if args.no_context_compaction is not None
                    else _env_bool("TRACEFIX_CONTEXT_ENABLED", True)
                ),
                context_window_tokens=_number_or_default(
                    args.context_window_tokens,
                    "TRACEFIX_CONTEXT_WINDOW_TOKENS",
                    int,
                    1_000_000,
                ),
                compaction_trigger_tokens=_number_or_default(
                    args.context_trigger_tokens,
                    "TRACEFIX_CONTEXT_TRIGGER_TOKENS",
                    int,
                    32_000,
                ),
                retain_ratio=_number_or_default(
                    args.context_retain_ratio,
                    "TRACEFIX_CONTEXT_RETAIN_RATIO",
                    float,
                    0.375,
                ),
            ),
            repo_map=RepoMapConfig(
                enabled=(
                    False
                    if args.no_repo_map
                    else (True if args.repo_map else _env_bool("TRACEFIX_REPO_MAP_ENABLED", True))
                ),
                max_chars=_number_or_default(
                    args.repo_map_max_chars, "TRACEFIX_REPO_MAP_MAX_CHARS", int, 12_000
                ),
            ),
        ),
    }


def _read_task(args: argparse.Namespace) -> str:
    """从互斥的文本参数或 UTF-8 文件获得任务描述。"""
    if args.task is not None:
        return args.task
    try:
        return args.task_file.expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"无法读取任务文件：{exc}") from exc


def _print_json(value: Any) -> None:
    """以 ASCII 安全 JSON 输出，避免 Windows 非 UTF-8 控制台因测试日志而中断。"""
    print(json.dumps(value, ensure_ascii=True, indent=2))


def _print_run_result(result: Any) -> None:
    """输出适合人阅读、且不包含供应商原始响应的运行摘要。"""
    print(f"状态: {result.status.value}")
    print(f"步骤: {result.step_count}")
    print(f"Token: 输入 {result.input_tokens} / 输出 {result.output_tokens}")
    context_metrics = result.context_metrics
    print(
        "上下文: "
        f"折叠 {context_metrics.compaction_count} 次 / "
        f"裁剪工具结果 {context_metrics.tool_results_pruned} 次 / "
        f"估算节省 {context_metrics.estimated_tokens_saved} Token"
    )
    presentation = getattr(result, "presentation_metrics", None)
    if presentation is not None:
        print(
            "工具结果视图: "
            f"精简 {presentation.compacted_result_count}/{presentation.result_count} 条 / "
            f"估算节省 {presentation.estimated_tokens_saved} Token"
        )
    if hasattr(result, "model_request_seconds"):
        print(
            "耗时: "
            f"模型 {result.model_request_seconds:.2f}s / "
            f"工具 {result.tool_execution_seconds:.2f}s / "
            f"索引 {result.repository_index_seconds:.2f}s"
        )
    if result.cost_complete:
        print(
            f"费用: ${result.cost_usd:.8f} / 约 ¥{result.cost_cny_estimate:.8f} "
            f"(汇率 {result.usd_cny_rate})"
        )
    else:
        print(f"费用: 已知部分 ${result.cost_usd:.8f}，供应商费用数据不完整")
    print(f"结果文件: {result.result_path}")
    print(f"补丁文件: {result.diff_path}")
    if result.final_output:
        print(f"Agent: {result.final_output}")
    error = getattr(result, "error", None)
    if isinstance(error, dict):
        code = error.get("code", "run_error")
        message = error.get("message", "运行失败，请检查 result.json 和 trajectory.jsonl")
        print(f"错误: [{code}] {message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """执行 CLI 并使用稳定退出码区分成功、失败和预算中断。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-real-tasks":
            tasks = load_real_issue_tasks(args.tasks, task_ids=tuple(args.task_id))
            validations = []
            for task in tasks:
                validation = task.validate_artifacts()
                if args.with_checkout:
                    checkout = (args.checkout_dir / task.id).resolve()
                    # 联网检出可能在中途被终端中断。若目录已经存在，先严格验证
                    # 它是否仍是该任务的固定 commit，而不是要求用户改用新目录。
                    if not checkout.exists():
                        checkout = task.prepare_checkout(checkout)
                    validation = task.validate_checkout(checkout)
                validations.append(validation.model_dump(mode="json"))
            # 输出结构化 JSON，便于把一次联网校验的结果直接归档。
            _print_json(validations)
            return 0

        if args.command == "validate-real-behavior":
            tasks = load_real_issue_tasks(args.tasks, task_ids=tuple(args.task_id))
            recipes = load_environment_recipes(args.recipes)
            validations = []
            result_path = args.output_dir.resolve() / "behavior-validation.json"
            result_path.parent.mkdir(parents=True, exist_ok=True)

            def persist_partial_results() -> None:
                """每题完成即写入汇总，意外中断时仍保留已获得的验收证据。"""
                result_path.write_text(
                    json.dumps(validations, ensure_ascii=False, indent=2), encoding="utf-8"
                )

            for task in tasks:
                recipe = recipes.get(task.id)
                if recipe is not None and not recipe.supports_current_platform():
                    validations.append(
                        {
                            "task_id": task.id,
                            "eligible_for_llm_prescreen": False,
                            "environment_or_execution_error": (
                                "platform is unsupported by task recipe"
                            ),
                        }
                    )
                    persist_partial_results()
                    continue
                try:
                    test_python = (
                        args.test_python.expanduser().resolve()
                        if args.test_python is not None
                        else _real_task_python(args.test_env_root, task.id)
                    )
                    if not test_python.is_file():
                        raise BenchmarkError(
                            "explicit test Python executable does not exist",
                            context={"path": str(test_python)},
                        )
                    if recipe is not None:
                        version_command = (
                            "import sys; "
                            "print(f'{sys.version_info.major}.{sys.version_info.minor}')"
                        )
                        version_result = subprocess.run(
                            [str(test_python), "-c", version_command],
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            timeout=20,
                            check=False,
                            shell=False,
                        )
                        compatible = recipe.supports_python(version_result.stdout.strip())
                        if version_result.returncode != 0 or not compatible:
                            raise BenchmarkError(
                                "explicit test Python is incompatible with task recipe"
                            )
                    validation = validate_real_task_behavior(
                        task,
                        source=(args.source_root / task.id).resolve(),
                        test_python=test_python,
                        output_dir=args.output_dir.resolve(),
                        recipe=recipe,
                    )
                    validations.append(validation.model_dump(mode="json"))
                except (BenchmarkError, ValueError) as exc:
                    # 一个历史仓库的依赖问题不能阻断其他候选的资格判断。
                    validations.append(
                        {
                            "task_id": task.id,
                            "eligible_for_llm_prescreen": False,
                            "environment_or_execution_error": str(exc),
                        }
                    )
                persist_partial_results()
            persist_partial_results()
            _print_json(validations)
            print(f"验收记录: {result_path}", file=sys.stderr)
            return 0

        if args.command == "prepare-real-environments":
            summary = RealEnvironmentPreparer().prepare(
                EnvironmentPreparationConfig(
                    tasks_dir=args.tasks,
                    source_root=args.source_root,
                    environment_root=args.test_env_root,
                    output_dir=args.output_dir,
                    task_ids=tuple(args.task_id),
                    python_executable=args.python,
                    index_url=args.index_url,
                    recipes_dir=args.recipes,
                    min_free_gib=args.min_free_gib,
                    allow_create_interpreter=not args.no_create_interpreter,
                )
            )
            ready = sum(item.status in {"ready", "reused"} for item in summary.results)
            print(f"真实任务环境准备完成: {ready}/{len(summary.results)} 可用")
            print(f"汇总文件: {summary.summary_path}")
            return 0

        if args.command == "inspect-real-environments":
            report = inspect_storage(args.environment_root)
            interpreters = discover_interpreters(args.environment_root)
            _print_json(
                {
                    "storage": report.model_dump(mode="json"),
                    "interpreters": [item.model_dump() for item in interpreters],
                }
            )
            return 0

        if args.command == "p2-dry-run":
            path = write_p2_dry_run(
                P2ProtocolConfig(
                    tasks_dir=args.tasks,
                    recipes_dir=args.recipes,
                    source_root=args.source_root,
                    test_env_root=args.test_env_root,
                    p1_evidence_path=args.p1_evidence,
                    output_dir=args.output_dir,
                )
            )
            print("P2 零费用演练完成：未初始化供应商客户端，正式实验参数仍缺失。")
            print(f"协议文件: {path}")
            return 0

        if args.command == "p2-run":
            formal = None
            if args.mode == "formal":
                cap = args.total_cost_cap_cny if args.currency == "CNY" else args.total_cost_cap_usd
                input_price = (
                    args.input_cache_miss_cost_per_million
                    if args.currency == "CNY"
                    else args.input_cost_per_million_usd
                )
                output_price = (
                    args.output_cost_per_million
                    if args.currency == "CNY"
                    else args.output_cost_per_million_usd
                )
                formal = P2FormalRunRequirements(
                    model_name=args.model_name,
                    provider=args.provider,
                    pricing_source=args.pricing_source,
                    total_cost_cap_usd=cap,
                    input_cost_per_million_usd=input_price,
                    output_cost_per_million_usd=output_price,
                    currency=args.currency,
                    input_cache_hit_cost_per_million=args.input_cache_hit_cost_per_million,
                    input_cache_miss_cost_per_million=args.input_cache_miss_cost_per_million,
                    output_cost_per_million=args.output_cost_per_million,
                    prior_calculated_amount=args.prior_calculated_amount,
                    prior_unsettled_reservation=args.prior_unsettled_reservation,
                    campaign_ledger_path=args.campaign_ledger,
                )
            config = P2ProtocolConfig(
                tasks_dir=args.tasks,
                recipes_dir=args.recipes,
                source_root=args.source_root,
                test_env_root=args.test_env_root,
                p1_evidence_path=args.p1_evidence,
                formal=formal,
            )
            run = run_p2_formal if args.mode == "formal" else run_p2_simulation
            summary = run(config, experiment_dir=args.experiment_dir)
            label = "P2 正式实验" if args.mode == "formal" else "P2 零费用工程演练"
            print(f"{label}: {summary.completed_count}/{summary.trial_count}")
            print(f"恢复复用: {summary.resumed_count}; 汇总: {summary.summary_path}")
            return 0

        if args.command == "p2-check":
            path = write_p2_check(
                P2ProtocolConfig(
                    tasks_dir=args.tasks,
                    recipes_dir=args.recipes,
                    source_root=args.source_root,
                    test_env_root=args.test_env_root,
                    p1_evidence_path=args.p1_evidence,
                    output_dir=args.output_dir,
                )
            )
            print(f"P2 输入检查通过：{path}")
            return 0

        if args.command == "p2-summarize":
            path = write_p2_summary(args.experiment_dir)
            print(f"P2 汇总: {path}")
            return 0

        if args.command == "p2-reconcile":
            path = write_p2_reconciliation(
                args.experiment_dir, bill_path=args.bill, api_key_name=args.api_key_name
            )
            print(f"P2 对账记录: {path}")
            return 0

        if args.command == "clean-real-artifacts":
            preview = (
                apply_cleanup(args.environment_root)
                if args.apply
                else preview_cleanup(args.environment_root)
            )
            _print_json({"applied": args.apply, **preview.model_dump(mode="json")})
            return 0

        if args.command == "retrieval-eval":
            summary = RetrievalEvaluator().run(
                RetrievalEvaluationConfig(
                    tasks_dir=args.tasks,
                    source_root=args.source_root,
                    output_dir=args.output_dir,
                    task_ids=tuple(args.task_id),
                    repo_map=RepoMapConfig(max_chars=args.repo_map_max_chars),
                )
            )
            print(f"离线检索评测完成: {summary.task_count} 题，不调用 LLM")
            print(
                "Hit@5: "
                f"baseline {summary.baseline_metrics.hit_at_5:.1%}；"
                f"repo_map {summary.repo_map_metrics.hit_at_5:.1%}"
            )
            print(f"汇总文件: {summary.summary_path}")
            return 0

        if args.command == "collect-real-candidates":
            config = CandidateCollectionConfig(
                dataset=args.dataset,
                revision=args.revision,
                source=args.source,
                output_dir=args.output_dir,
                per_repository=args.per_repository,
                min_source_files=args.min_source_files,
                repositories=tuple(args.repositories)
                if args.repositories
                else CandidateCollectionConfig().repositories,
            )
            result = collect_candidates(config)
            print(f"候选池生成完成: {len(result.selected)} 题")
            print(f"清单文件: {result.output_path}")
            return 0

        if args.command == "screen-real-candidates":
            task_ids = list(args.task_id)
            if args.candidate_pool:
                try:
                    pool = json.loads(args.candidate_pool.read_text(encoding="utf-8"))
                    task_ids.extend(item["instance_id"] for item in pool.get("selected", ()))
                except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
                    raise ValueError(
                        "候选池清单必须是 collect-real-candidates 生成的 JSON"
                    ) from exc
            summary = RetrievalEvaluator().run(
                RetrievalEvaluationConfig(
                    tasks_dir=args.tasks,
                    source_root=args.source_root,
                    output_dir=args.output_dir,
                    task_ids=tuple(dict.fromkeys(task_ids)),
                    repo_map=RepoMapConfig(max_chars=args.repo_map_max_chars),
                )
            )
            print(f"候选结构筛选完成: {summary.task_count} 题，不调用 LLM")
            print(f"Repo Map Hit@5: {summary.repo_map_metrics.hit_at_5:.1%}")
            print(f"汇总文件: {summary.summary_path}")
            return 0

        shared = _resolve_shared(
            args,
            real_issue_budget=args.command
            in {"real-prescreen", "real-repo-map-prescreen", "real-paired-eval"},
        )
        if args.command == "run":
            result = TraceFixRunner().run(
                RunConfig(repo=args.repo, task=_read_task(args), **shared)
            )
            _print_run_result(result)
            if result.status is AgentStatus.COMPLETED:
                return 0
            return 2 if result.status is AgentStatus.INTERRUPTED else 1

        if args.command in {
            "real-prescreen",
            "real-repo-map-prescreen",
            "real-paired-eval",
        }:
            # 真实任务按题选择独立解释器和 bootstrap，因此不采用 shared 中的
            # 单一 test-python 参数；其余模型与预算字段保持完全一致。
            shared.pop("test_python_executable", None)
            shared.pop("test_pythonpath_entries", None)
            real_config = RealExperimentConfig(
                tasks_dir=args.tasks,
                source_root=args.source_root,
                test_env_root=args.test_env_root,
                task_ids=tuple(args.task_id),
                **shared,
            )
            if args.command == "real-prescreen":
                summary = RealPrescreenRunner().run(real_config)
                print(
                    f"真实任务预筛选完成: {len(summary.eligible_task_ids)}/"
                    f"{len(summary.results)} 达到正式实验门槛"
                )
                print(f"汇总文件: {summary.summary_path}")
                return 0
            if args.command == "real-repo-map-prescreen":
                if args.repo_map or args.no_repo_map:
                    raise ValueError(
                        "real-repo-map-prescreen 会自动运行关闭和开启 Repo Map 两组，"
                        "不能额外传 --repo-map 或 --no-repo-map"
                    )
                summary = RealRepoMapPrescreenRunner().run(real_config)
                print(
                    "Repo Map 预筛选完成: "
                    f"关闭 {summary.control.resolved_count}/{summary.control.trial_count}；"
                    f"开启 {summary.treatment.resolved_count}/{summary.treatment.trial_count}"
                )
                print(f"汇总文件: {summary.summary_path}")
                return 0
            payload = RealPrescreenSummary.model_validate_json(
                args.prescreen_summary.read_text(encoding="utf-8")
            )
            summary = RealPairedExperimentRunner().run_paired(
                real_config,
                eligible_task_ids=payload.eligible_task_ids,
                repetitions=args.repetitions,
            )
            print(f"真实任务配对实验完成: {len(summary.trials)} trials")
            print(f"汇总文件: {summary.summary_path}")
            return 0

        benchmark_config = BenchmarkConfig(
            tasks_dir=args.tasks,
            limit=args.limit,
            task_ids=tuple(args.task_id),
            **shared,
        )
        if args.command == "paired-eval":
            summary = PairedExperimentRunner().run(
                PairedExperimentConfig(
                    benchmark=benchmark_config,
                    repetitions=args.repetitions,
                    trigger_tokens=32_000,
                )
            )
            print(
                "配对实验完成: "
                f"关闭压缩 {summary.control.resolved_count}/"
                f"{summary.control.trial_count}；"
                f"32k 压缩 {summary.treatment.resolved_count}/"
                f"{summary.treatment.trial_count}"
            )
            print(f"汇总文件: {summary.summary_path}")
            return 0

        summary = BenchmarkRunner().run(benchmark_config)
        print(
            f"评测完成: {summary.resolved_count}/{summary.task_count} ({summary.resolved_rate:.1%})"
        )
        print(f"汇总文件: {summary.summary_path}")
        return 0 if summary.resolved_count == summary.task_count else 1
    except (TraceFixError, ValidationError, ValueError) as exc:
        print(f"TraceFix 错误: {exc}", file=sys.stderr)
        return 2


def _real_task_python(root: Path, task_id: str) -> Path:
    """按平台定位一个真实任务的独立虚拟环境解释器。"""
    try:
        return resolve_managed_environment_python(root, task_id)
    except BenchmarkError as exc:
        raise ValueError(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
