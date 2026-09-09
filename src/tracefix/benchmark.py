"""可复现合成任务的加载、准备和批量评测。"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from tracefix.agent import AgentConfig, AgentStatus
from tracefix.context import ContextMetrics
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


class GeneratedTextSeries(BaseModel):
    """为长上下文基准声明一组可复现、无可执行代码的文本文件。"""

    model_config = ConfigDict(extra="forbid")

    directory: str
    filename_prefix: str = "document_"
    filename_suffix: str = ".md"
    count: int = Field(ge=1, le=64)
    lines_per_file: int = Field(ge=1, le=200)
    header_template: str = ""
    line_template: str = Field(min_length=1)
    footer_template: str = ""

    @model_validator(mode="after")
    def validate_paths_and_names(self) -> GeneratedTextSeries:
        """限制生成位置和文件名，防止任务清单借生成器逃逸目录。"""
        directory = Path(self.directory)
        if (
            directory.is_absolute()
            or ".." in directory.parts
            or any(part.casefold() == ".git" for part in directory.parts)
        ):
            raise ValueError("generated text directory must be a safe relative path")
        for value in (self.filename_prefix, self.filename_suffix):
            if not value or any(separator in value for separator in ("/", "\\", "\x00")):
                raise ValueError("generated text filename parts cannot contain separators")
        return self


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
    hidden_tests_dir: str | None = None
    generated_text_series: tuple[GeneratedTextSeries, ...] = ()
    task_dir: Path

    @model_validator(mode="after")
    def validate_relative_paths(self) -> BenchmarkTask:
        """清单中的路径只能指向任务目录内部。"""
        values = [*self.expected_files, self.repository_dir, self.gold_patch]
        if self.hidden_tests_dir is not None:
            values.append(self.hidden_tests_dir)
        for value in values:
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

    @property
    def hidden_tests_path(self) -> Path | None:
        """返回不会复制给 Agent、仅供评测器最终判定的测试目录。"""
        if self.hidden_tests_dir is None:
            return None
        return (self.task_dir / self.hidden_tests_dir).resolve()

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
        if task.hidden_tests_path is not None:
            checked_paths["hidden_tests_dir"] = task.hidden_tests_path
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
        if task.hidden_tests_path is not None and not task.hidden_tests_path.is_dir():
            raise BenchmarkError(
                "benchmark hidden tests directory is missing",
                context={"task_id": task.id, "path": str(task.hidden_tests_path)},
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

        self._materialize_generated_text(target)

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

    def _materialize_generated_text(self, target: Path) -> None:
        """根据受限清单生成长文档；这里只做文本替换，不执行任务内代码。"""
        for series in self.generated_text_series:
            directory = (target / series.directory).resolve()
            if not directory.is_relative_to(target):
                raise BenchmarkError(
                    "generated text directory escapes repository",
                    context={"task_id": self.id, "directory": series.directory},
                )
            directory.mkdir(parents=True, exist_ok=True)
            for index in range(1, series.count + 1):
                filename = f"{series.filename_prefix}{index:02d}{series.filename_suffix}"
                path = (directory / filename).resolve()
                if not path.is_relative_to(target) or path.exists():
                    raise BenchmarkError(
                        "generated text path is unsafe or already exists",
                        context={"task_id": self.id, "path": str(path)},
                    )
                lines: list[str] = []
                if series.header_template:
                    lines.append(self._render_generated_template(series.header_template, index))
                lines.extend(
                    self._render_generated_template(series.line_template, index, line=line)
                    for line in range(1, series.lines_per_file + 1)
                )
                if series.footer_template:
                    lines.append(self._render_generated_template(series.footer_template, index))
                try:
                    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                except OSError as exc:
                    raise BenchmarkError(
                        f"cannot generate benchmark text: {exc}",
                        context={"task_id": self.id, "path": str(path)},
                    ) from exc

    @staticmethod
    def _render_generated_template(
        template: str,
        index: int,
        *,
        line: int | None = None,
    ) -> str:
        """只替换固定占位符，避免引入通用模板执行能力。"""
        value = template.replace("{index}", str(index)).replace(
            "{index02}", f"{index:02d}"
        )
        return value.replace("{line}", str(line)) if line is not None else value

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
    """单个任务的 Agent、公开测试与独立验收三层判定。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    title: str
    resolved: bool
    run: RunResult
    agent_completed: bool
    public_tests_passed: bool
    independent_tests_passed: bool | None = None
    tests_modified: bool = False
    changed_test_files: tuple[str, ...] = ()
    public_verification: ToolResult | None = None
    independent_verification: ToolResult | None = None
    # 保留旧字段作为公开测试结果别名，方便已有分析脚本平滑迁移。
    verification: ToolResult | None = None
    verification_kind: str = "visible"


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
    agent_completed_count: int = Field(ge=0)
    public_tests_passed_count: int = Field(ge=0)
    independent_tests_passed_count: int = Field(ge=0)
    tests_modified_count: int = Field(ge=0)
    total_input_tokens: int = Field(ge=0)
    total_output_tokens: int = Field(ge=0)
    total_steps: int = Field(ge=0)
    total_agent_test_runs: int = Field(ge=0)
    total_cost_usd: float = Field(ge=0)
    cost_complete: bool
    usd_cny_rate: float = Field(gt=0)
    total_cost_cny_estimate: float | None = Field(default=None, ge=0)
    context_metrics: ContextMetrics = Field(default_factory=ContextMetrics)
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
            public_verification, independent_verification = self._verify(
                task, run_result
            )
            changed_test_files = self._changed_test_files(task, run_result)
            agent_completed = run_result.status is AgentStatus.COMPLETED
            public_passed = bool(public_verification and public_verification.success)
            independent_passed = (
                bool(independent_verification and independent_verification.success)
                if task.hidden_tests_path is not None
                else None
            )
            # resolved 要求 Agent 正常结束、公开测试通过、未篡改测试；若任务有
            # 隐藏验收，还必须独立验收通过。三个信号仍分别保存，不能互相替代。
            resolved = (
                agent_completed
                and public_passed
                and not changed_test_files
                and independent_passed is not False
            )
            results.append(
                BenchmarkTaskResult(
                    task_id=task.id,
                    title=task.title,
                    resolved=resolved,
                    run=run_result,
                    agent_completed=agent_completed,
                    public_tests_passed=public_passed,
                    independent_tests_passed=independent_passed,
                    tests_modified=bool(changed_test_files),
                    changed_test_files=changed_test_files,
                    public_verification=public_verification,
                    independent_verification=independent_verification,
                    verification=public_verification,
                    verification_kind=(
                        "visible_and_hidden" if task.hidden_tests_path else "visible"
                    ),
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
    def _verify(
        task: BenchmarkTask, run: RunResult
    ) -> tuple[ToolResult | None, ToolResult | None]:
        """分别运行公开测试和 Agent 不可见的独立验收。"""
        if run.workspace is None:
            return None, None
        workspace = Path(run.workspace)
        tool = RunTestsTool(workspace, default_timeout_seconds=120)
        public_result = tool.execute(
            ToolCall(
                id=f"verify-public-{task.id}",
                name=tool.spec.name,
                arguments={"command": task.test_command, "timeout_seconds": 120},
            )
        ).model_copy(
            update={"metadata": {"verification_kind": "public"}}
        )
        independent_result: ToolResult | None = None
        if task.hidden_tests_path is not None:
            # 隐藏测试只在 Agent 完成后复制，模型的 search/read 工具无法提前看到。
            verification_dir = workspace / f".tracefix_verification_{uuid4().hex}"
            try:
                shutil.copytree(task.hidden_tests_path, verification_dir)
            except OSError as exc:
                raise BenchmarkError(
                    f"cannot prepare hidden verification tests: {exc}",
                    context={"task_id": task.id},
                ) from exc
            independent_result = tool.execute(
                ToolCall(
                    id=f"verify-independent-{task.id}",
                    name=tool.spec.name,
                    arguments={
                        "command": f"pytest -q {verification_dir.name}",
                        "timeout_seconds": 120,
                    },
                )
            ).model_copy(
                update={"metadata": {"verification_kind": "independent_hidden"}}
            )
        return public_result, independent_result

    @staticmethod
    def _changed_test_files(
        task: BenchmarkTask, run: RunResult
    ) -> tuple[str, ...]:
        """识别测试与测试发现配置的净修改，防止通过篡改验收。"""
        protected_config_names = {"pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"}
        changed: list[str] = []
        for value in run.changed_files:
            path = Path(value)
            parts = {part.casefold() for part in path.parts}
            name = path.name.casefold()
            # pytest 与 Python 自动生成的缓存不是 Agent 补丁，不能误判为测试篡改。
            if "__pycache__" in parts or ".pytest_cache" in parts or path.suffix == ".pyc":
                continue
            if (
                "tests" in parts
                or name.startswith("test_")
                or name in protected_config_names
            ):
                changed.append(path.as_posix())
        return tuple(sorted(set(changed)))

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
        context_metrics = ContextMetrics(
            preparation_count=sum(
                item.run.context_metrics.preparation_count for item in results
            ),
            compaction_count=sum(
                item.run.context_metrics.compaction_count for item in results
            ),
            tool_results_pruned=sum(
                item.run.context_metrics.tool_results_pruned for item in results
            ),
            messages_compacted=sum(
                item.run.context_metrics.messages_compacted for item in results
            ),
            batches_compacted=sum(
                item.run.context_metrics.batches_compacted for item in results
            ),
            estimated_tokens_saved=sum(
                item.run.context_metrics.estimated_tokens_saved for item in results
            ),
        )
        return BenchmarkSummary(
            batch_id=batch_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            model_name=model_name,
            task_count=len(results),
            resolved_count=resolved,
            resolved_rate=resolved / len(results) if results else 0.0,
            agent_completed_count=sum(item.agent_completed for item in results),
            public_tests_passed_count=sum(item.public_tests_passed for item in results),
            independent_tests_passed_count=sum(
                item.independent_tests_passed is True for item in results
            ),
            tests_modified_count=sum(item.tests_modified for item in results),
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
            context_metrics=context_metrics,
            results=tuple(results),
            summary_path=str(summary_path),
        )
