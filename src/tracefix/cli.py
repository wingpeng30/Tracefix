"""TraceFix 的非交互式命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tracefix.agent import AgentConfig, AgentStatus
from tracefix.benchmark import BenchmarkConfig, BenchmarkRunner
from tracefix.context import ContextConfig
from tracefix.exceptions import TraceFixError
from tracefix.paired import PairedExperimentConfig, PairedExperimentRunner
from tracefix.real_benchmark import load_real_issue_tasks
from tracefix.real_experiment import (
    RealExperimentConfig,
    RealPairedExperimentRunner,
    RealPrescreenRunner,
    RealPrescreenSummary,
    validate_real_task_behavior,
)
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

    prescreen_parser = subparsers.add_parser(
        "real-prescreen", help="真实 Issue 的单次 32k 压缩触发预筛选"
    )
    _add_real_task_locations(prescreen_parser)
    _add_shared_options(prescreen_parser)

    real_paired_parser = subparsers.add_parser(
        "real-paired-eval", help="只对至少三道已入选真实任务执行正式配对实验"
    )
    _add_real_task_locations(real_paired_parser)
    real_paired_parser.add_argument(
        "--prescreen-summary", type=Path, required=True, help="预筛选 summary JSON"
    )
    real_paired_parser.add_argument("--repetitions", type=int, default=3)
    _add_shared_options(real_paired_parser)
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
        "test_python_executable": (
            Path(value)
            if (
                value := _first(
                    args.test_python, "TRACEFIX_TEST_PYTHON", None
                )
            )
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
        if args.command == "validate-real-tasks":
            tasks = load_real_issue_tasks(args.tasks, task_ids=tuple(args.task_id))
            validations = []
            for task in tasks:
                validation = task.validate_artifacts()
                if args.with_checkout:
                    checkout = task.prepare_checkout(args.checkout_dir / task.id)
                    validation = task.validate_checkout(checkout)
                validations.append(validation.model_dump(mode="json"))
            # 输出结构化 JSON，便于把一次联网校验的结果直接归档。
            print(json.dumps(validations, ensure_ascii=False, indent=2))
            return 0

        if args.command == "validate-real-behavior":
            tasks = load_real_issue_tasks(args.tasks, task_ids=tuple(args.task_id))
            validations = []
            for task in tasks:
                validation = validate_real_task_behavior(
                    task,
                    source=(args.source_root / task.id).resolve(),
                    test_python=_real_task_python(args.test_env_root, task.id),
                    output_dir=args.output_dir.resolve(),
                )
                validations.append(validation.model_dump(mode="json"))
            result_path = args.output_dir.resolve() / "behavior-validation.json"
            result_path.write_text(
                json.dumps(validations, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(validations, ensure_ascii=False, indent=2))
            print(f"验收记录: {result_path}", file=sys.stderr)
            return 0

        shared = _resolve_shared(args)
        if args.command == "run":
            result = TraceFixRunner().run(
                RunConfig(repo=args.repo, task=_read_task(args), **shared)
            )
            _print_run_result(result)
            if result.status is AgentStatus.COMPLETED:
                return 0
            return 2 if result.status is AgentStatus.INTERRUPTED else 1

        if args.command in {"real-prescreen", "real-paired-eval"}:
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

        summary = BenchmarkRunner().run(
            benchmark_config
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


def _real_task_python(root: Path, task_id: str) -> Path:
    """按平台定位一个真实任务的独立虚拟环境解释器。"""
    environment = root.expanduser().resolve() / task_id
    candidates = (
        environment / "Scripts" / "python.exe",
        environment / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError(f"找不到任务 {task_id} 的测试解释器：{environment}")


if __name__ == "__main__":
    raise SystemExit(main())
