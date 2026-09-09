"""可复现合成任务的加载、准备和批量评测。"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from tracefix.agent import AgentConfig
from tracefix.exceptions import BenchmarkError, sanitize_payload
from tracefix.messages import ToolCall
from tracefix.runtime import (
    DEFAULT_MODEL_NAME,
    DEFAULT_USD_CNY_RATE,
    RunConfig,
    RunResult,
    TraceFixRunner,
)
from tracefix.tools import RunTestsTool, ToolResult


class BenchmarkTask(BaseModel):
    """一个磁盘基准任务的描述、测试命令和标准答案位置。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    test_command: str = Field(default="pytest -q", min_length=1)
    expected_files: tuple[str, ...] = Field(min_length=1)
    repository_dir: str = "repo"
    gold_patch: str = "gold.patch"
    task_dir: Path

    @model_validator(mode="after")
    def validate_relative_paths(self) -> BenchmarkTask:
        """清单中的路径只能指向任务目录内部。"""
        for value in (*self.expected_files, self.repository_dir, self.gold_patch):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"benchmark paths must be relative: {value}")
        return self

    @property
    def repository_path(self) -> Path:
        """返回不含 Git 元数据的初始 Bug 仓库模板目录。"""
        return (self.task_dir / self.repository_dir).resolve()

    @property
    def gold_patch_path(self) -> Path:
        """返回能让参考测试通过的标准补丁路径。"""
        return (self.task_dir / self.gold_patch).resolve()

    @classmethod
    def load(cls, task_dir: str | Path) -> BenchmarkTask:
        """从任务目录的 task.json 加载并验证一个任务。"""
        root = Path(task_dir).expanduser().resolve()
        manifest = root / "task.json"
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BenchmarkError(
                f"cannot load benchmark manifest: {exc}",
                context={"manifest": str(manifest)},
            ) from exc
        try:
            task = cls.model_validate({**payload, "task_dir": root})
        except Exception as exc:
            raise BenchmarkError(
                f"invalid benchmark manifest: {exc}",
                context={"manifest": str(manifest)},
            ) from exc
        # 仅检查字符串中的 ``..`` 不足以阻止符号链接逃逸；解析真实路径后再次限定边界。
        checked_paths = {
            "repository_dir": task.repository_path,
            "gold_patch": task.gold_patch_path,
            **{
                f"expected_files[{index}]": (task.repository_path / value).resolve()
                for index, value in enumerate(task.expected_files)
            },
        }
        for label, path in checked_paths.items():
            if not path.is_relative_to(root):
                raise BenchmarkError(
                    "benchmark path escapes its task directory",
                    context={"task_id": task.id, "field": label, "path": str(path)},
                )
        if not task.repository_path.is_dir():
            raise BenchmarkError(
                "benchmark repository directory is missing",
                context={"task_id": task.id, "path": str(task.repository_path)},
            )
        if not task.gold_patch_path.is_file():
            raise BenchmarkError(
                "benchmark gold patch is missing",
                context={"task_id": task.id, "path": str(task.gold_patch_path)},
            )
        return task

    def prepare_source_repository(self, destination: str | Path) -> Path:
        """复制模板并创建只有一个初始提交的干净 Git 源仓库。"""
        target = Path(destination).expanduser().resolve()
        if target.exists():
            raise BenchmarkError(
                "benchmark source destination already exists",
                context={"path": str(target)},
            )
        try:
            shutil.copytree(self.repository_path, target)
        except OSError as exc:
            raise BenchmarkError(
                f"cannot copy benchmark repository: {exc}",
                context={"task_id": self.id, "path": str(target)},
            ) from exc

        self._run_git(["init", "--quiet"], target)
        self._run_git(["add", "--all"], target)
        self._run_git(
            [
                "-c",
                "user.name=TraceFix Benchmark",
                "-c",
                "user.email=tracefix@example.invalid",
                "commit",
                "--quiet",
                "-m",
                f"Initial fixture for {self.id}",
            ],
            target,
        )
        return target

    @staticmethod
    def _run_git(arguments: list[str], cwd: Path) -> None:
        """执行任务准备所需 Git 命令，不修改用户的全局 Git 配置。"""
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BenchmarkError(f"cannot prepare benchmark Git repository: {exc}") from exc
        if result.returncode != 0:
            raise BenchmarkError(
                "cannot prepare benchmark Git repository",
                context={"returncode": result.returncode, "stderr": result.stderr.strip()},
            )


class BenchmarkConfig(BaseModel):
    """一次串行基准评测的任务选择、模型和预算配置。"""

    model_config = ConfigDict(extra="forbid")

    tasks_dir: Path = Path("benchmarks/tasks")
    output_dir: Path = Path("runs")
    model_name: str = DEFAULT_MODEL_NAME
    env_file: Path | None = Path(".env")
    usd_cny_rate: float = Field(default=DEFAULT_USD_CNY_RATE, gt=0)
    limit: int | None = Field(default=None, ge=1)
    task_ids: tuple[str, ...] = ()
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    per_request_output_tokens: int = Field(default=4_096, ge=1)
    agent_config: AgentConfig = Field(default_factory=AgentConfig)


class BenchmarkTaskResult(BaseModel):
    """单个任务的 Agent 运行结果和独立测试判定。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    title: str
    resolved: bool
    run: RunResult
    verification: ToolResult | None = None


class BenchmarkSummary(BaseModel):
    """一批任务的解决率、用量、费用与逐任务结果。"""

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    started_at: datetime
    finished_at: datetime
    model_name: str
    task_count: int = Field(ge=0)
    resolved_count: int = Field(ge=0)
    resolved_rate: float = Field(ge=0, le=1)
    total_input_tokens: int = Field(ge=0)
    total_output_tokens: int = Field(ge=0)
    total_steps: int = Field(ge=0)
    total_agent_test_runs: int = Field(ge=0)
    total_cost_usd: float = Field(ge=0)
    cost_complete: bool
    usd_cny_rate: float = Field(gt=0)
    total_cost_cny_estimate: float | None = Field(default=None, ge=0)
    results: tuple[BenchmarkTaskResult, ...]
    summary_path: str


def load_benchmark_tasks(
    tasks_dir: str | Path,
    *,
    task_ids: tuple[str, ...] = (),
    limit: int | None = None,
) -> tuple[BenchmarkTask, ...]:
    """按目录名稳定排序加载任务，并应用 ID 与数量筛选。"""
    root = Path(tasks_dir).expanduser().resolve()
    if not root.is_dir():
        raise BenchmarkError("tasks directory does not exist", context={"path": str(root)})
    selected_ids = set(task_ids)
    tasks = [
        BenchmarkTask.load(path)
        for path in sorted(root.iterdir(), key=lambda item: item.name)
        if path.is_dir() and (path / "task.json").is_file()
    ]
    if selected_ids:
        tasks = [task for task in tasks if task.id in selected_ids]
        missing = selected_ids.difference(task.id for task in tasks)
        if missing:
            raise BenchmarkError(
                "unknown benchmark task IDs",
                context={"missing_task_ids": sorted(missing)},
            )
    if limit is not None:
        tasks = tasks[:limit]
    if not tasks:
        raise BenchmarkError("no benchmark tasks were selected", context={"path": str(root)})
    return tuple(tasks)


class BenchmarkRunner:
    """串行运行基准任务，并在 Agent 结束后独立执行参考测试。"""

    def __init__(self, runner: TraceFixRunner | None = None) -> None:
        self.runner = runner or TraceFixRunner()

    def run(self, config: BenchmarkConfig) -> BenchmarkSummary:
        """运行选中的任务并持续覆盖写入可恢复的 summary.json。"""
        tasks = load_benchmark_tasks(
            config.tasks_dir,
            task_ids=config.task_ids,
            limit=config.limit,
        )
        batch_id = f"eval-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        batch_dir = config.output_dir.expanduser().resolve() / batch_id
        source_dir = batch_dir / "sources"
        run_output_dir = batch_dir / "runs"
        summary_path = batch_dir / "summary.json"
        source_dir.mkdir(parents=True, exist_ok=False)
        started_at = datetime.now(UTC)
        results: list[BenchmarkTaskResult] = []

        for task in tasks:
            source = task.prepare_source_repository(source_dir / task.id)
            run_result = self.runner.run(
                RunConfig(
                    repo=source,
                    task=task.description,
                    model_name=config.model_name,
                    output_dir=run_output_dir,
                    env_file=config.env_file,
                    usd_cny_rate=config.usd_cny_rate,
                    llm_timeout_seconds=config.llm_timeout_seconds,
                    llm_max_retries=config.llm_max_retries,
                    per_request_output_tokens=config.per_request_output_tokens,
                    agent_config=config.agent_config.model_copy(deep=True),
                )
            )
            verification = self._verify(task, run_result)
            results.append(
                BenchmarkTaskResult(
                    task_id=task.id,
                    title=task.title,
                    resolved=bool(verification and verification.success),
                    run=run_result,
                    verification=verification,
                )
            )
            # 每个任务后写一次汇总，长批次被中断时仍可读取已完成结果。
            summary = self._build_summary(
                batch_id=batch_id,
                started_at=started_at,
                model_name=config.model_name,
                usd_cny_rate=config.usd_cny_rate,
                results=results,
                summary_path=summary_path,
            )
            summary_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")

        return summary

    @staticmethod
    def _verify(task: BenchmarkTask, run: RunResult) -> ToolResult | None:
        """在 Agent 循环之外运行参考测试，不占用 Agent 测试预算。"""
        if run.workspace is None:
            return None
        tool = RunTestsTool(Path(run.workspace), default_timeout_seconds=120)
        return tool.execute(
            ToolCall(
                id=f"verify-{task.id}",
                name=tool.spec.name,
                arguments={"command": task.test_command, "timeout_seconds": 120},
            )
        )

    @staticmethod
    def _build_summary(
        *,
        batch_id: str,
        started_at: datetime,
        model_name: str,
        usd_cny_rate: float,
        results: list[BenchmarkTaskResult],
        summary_path: Path,
    ) -> BenchmarkSummary:
        """从逐任务结果计算可复现的基础指标。"""
        total_usd = sum(item.run.cost_usd for item in results)
        cost_complete = all(item.run.cost_complete for item in results)
        resolved = sum(item.resolved for item in results)
        payload: dict[str, JsonValue] = {
            "total_usd": total_usd,
            "cost_complete": cost_complete,
        }
        sanitized = sanitize_payload(payload)
        assert isinstance(sanitized, dict)
        return BenchmarkSummary(
            batch_id=batch_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            model_name=model_name,
            task_count=len(results),
            resolved_count=resolved,
            resolved_rate=resolved / len(results) if results else 0.0,
            total_input_tokens=sum(item.run.input_tokens for item in results),
            total_output_tokens=sum(item.run.output_tokens for item in results),
            total_steps=sum(item.run.step_count for item in results),
            total_agent_test_runs=sum(item.run.test_runs for item in results),
            total_cost_usd=float(sanitized["total_usd"]),
            cost_complete=bool(sanitized["cost_complete"]),
            usd_cny_rate=usd_cny_rate,
            total_cost_cny_estimate=(
                round(total_usd * usd_cny_rate, 8) if cost_complete else None
            ),
            results=tuple(results),
            summary_path=str(summary_path),
        )
