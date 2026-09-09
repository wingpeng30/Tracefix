"""TraceFix 的非交互式命令行入口。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tracefix.agent import AgentConfig, AgentStatus
from tracefix.benchmark import BenchmarkConfig, BenchmarkRunner
from tracefix.context import ContextConfig
from tracefix.exceptions import TraceFixError
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
    parser.add_argument("--llm-timeout-seconds", type=float, help="单次模型请求超时")
    parser.add_argument("--llm-max-retries", type=int, help="LiteLLM 自动重试次数")
    parser.add_argument(
        "--per-request-output-tokens",
        type=int,
        help="单次模型响应的最大输出 Token",
    )
    parser.add_argument(
        "--no-context-compaction",
        action="store_true",
        default=None,
        help="关闭工具结果裁剪和历史折叠，用于运行未压缩对照组",
    )
    parser.add_argument("--context-window-tokens", type=int, help="模型单次请求硬窗口")
    parser.add_argument("--context-trigger-tokens", type=int, help="历史折叠软阈值")
    parser.add_argument(
        "--context-retain-ratio", type=float, help="折叠后保留近期轮次的比例"
    )
    parser.add_argument(
        "--record-request-views",
        action="store_true",
        default=None,
        help="在轨迹中保存脱敏后的实际模型请求视图（默认关闭）",
    )


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
    return parser


def _resolve_shared(args: argparse.Namespace) -> dict[str, Any]:
    """加载 .env 后合并 CLI、环境变量和默认值。"""
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
        "agent_config": AgentConfig(
            max_steps=_number_or_default(
                args.max_steps, "TRACEFIX_MAX_STEPS", int, 30
            ),
            max_input_tokens=_number_or_default(
                args.max_input_tokens, "TRACEFIX_MAX_INPUT_TOKENS", int, 80_000
            ),
            max_output_tokens=_number_or_default(
                args.max_output_tokens, "TRACEFIX_MAX_OUTPUT_TOKENS", int, 20_000
            ),
            wall_time_seconds=_number_or_default(
                args.wall_time_seconds, "TRACEFIX_WALL_TIME_SECONDS", int, 1_200
            ),
            max_test_runs=_number_or_default(
                args.max_test_runs, "TRACEFIX_MAX_TEST_RUNS", int, 8
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
        shared = _resolve_shared(args)
        if args.command == "run":
            result = TraceFixRunner().run(
                RunConfig(repo=args.repo, task=_read_task(args), **shared)
            )
            _print_run_result(result)
            if result.status is AgentStatus.COMPLETED:
                return 0
            return 2 if result.status is AgentStatus.INTERRUPTED else 1

        summary = BenchmarkRunner().run(
            BenchmarkConfig(
                tasks_dir=args.tasks,
                limit=args.limit,
                task_ids=tuple(args.task_id),
                **shared,
            )
        )
        print(
            f"评测完成: {summary.resolved_count}/{summary.task_count} "
            f"({summary.resolved_rate:.1%})"
        )
        print(f"汇总文件: {summary.summary_path}")
        return 0 if summary.resolved_count == summary.task_count else 1
    except (TraceFixError, ValidationError, ValueError) as exc:
        print(f"TraceFix 错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
