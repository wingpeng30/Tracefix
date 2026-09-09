"""TraceFix 单任务运行编排、隔离工作区和结果模型。"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from tracefix.agent import AgentConfig, AgentState, AgentStatus, MinimalAgent
from tracefix.context import ContextMetrics
from tracefix.exceptions import (
    RunConfigurationError,
    TraceFixError,
    TraceProtocolError,
    WorkspaceError,
    sanitize_payload,
)
from tracefix.messages import ToolCall
from tracefix.models import BaseLLM, LiteLLMAdapter, LLMConfig
from tracefix.provenance import RunProvenance, collect_run_provenance
from tracefix.tools import GetGitDiffTool, create_default_tool_registry
from tracefix.tracing import JSONLTraceSink, TraceEvent, TraceEventType

DEFAULT_MODEL_NAME = "deepseek/deepseek-v4-flash"
DEFAULT_USD_CNY_RATE = 7.20
DEFAULT_DEEPSEEK_API_BASE = "https://api.deepseek.com"

LLMFactory = Callable[[LLMConfig], BaseLLM]


def load_environment_file(env_file: str | Path | None) -> None:
    """加载可选 .env 且不覆盖现有环境，密钥仅进入当前进程环境。"""
    if env_file is None:
        return
    path = Path(env_file).expanduser().resolve()
    if not path.exists():
        return
    if not path.is_file():
        raise RunConfigurationError(
            "env_file must be a regular file",
            context={"env_file": str(path)},
        )
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError as exc:
        raise RunConfigurationError(
            "python-dotenv is required to load .env; install TraceFix with the 'llm' extra",
            context={"install_hint": "pip install -e .[llm]"},
        ) from exc
    load_dotenv(path, override=False)


class RunConfig(BaseModel):
    """运行一个真实 TraceFix 任务需要的完整、可序列化配置。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    repo: Path
    task: str = Field(min_length=1)
    model_name: str = Field(default=DEFAULT_MODEL_NAME, min_length=1)
    output_dir: Path = Path("runs")
    env_file: Path | None = Path(".env")
    usd_cny_rate: float = Field(default=DEFAULT_USD_CNY_RATE, gt=0)
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    per_request_output_tokens: int = Field(default=4_096, ge=1)
    agent_config: AgentConfig = Field(default_factory=AgentConfig)

    @model_validator(mode="after")
    def normalize_text(self) -> RunConfig:
        """去除命令行常见的首尾空白，避免产生空任务或错误模型名。"""
        # validate_assignment 会让普通赋值再次进入本校验器，因此直接设置已校验值。
        object.__setattr__(self, "task", self.task.strip())
        object.__setattr__(self, "model_name", self.model_name.strip())
        if not self.task:
            raise ValueError("task cannot be empty")
        if not self.model_name:
            raise ValueError("model_name cannot be empty")
        return self


class RunResult(BaseModel):
    """一次隔离 Agent 运行的状态、费用、补丁和产物索引。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    source_repo: str
    source_commit: str | None = None
    workspace: str | None = None
    model_name: str
    status: AgentStatus
    stop_reason: str | None = None
    final_output: str | None = None
    step_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    test_runs: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    cost_complete: bool
    usd_cny_rate: float = Field(gt=0)
    cost_cny_estimate: float | None = Field(default=None, ge=0)
    started_at: datetime
    finished_at: datetime
    duration_seconds: float = Field(ge=0)
    changed_files: tuple[str, ...] = ()
    trace_path: str
    diff_path: str
    result_path: str
    agent_config: AgentConfig
    context_metrics: ContextMetrics = Field(default_factory=ContextMetrics)
    provenance: RunProvenance
    error: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def validate_time_and_cost(self) -> RunResult:
        """确保时区和费用完整性标记不会互相矛盾。"""
        if self.started_at.tzinfo is None or self.finished_at.tzinfo is None:
            raise ValueError("run timestamps must include timezone information")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot be earlier than started_at")
        if not self.cost_complete and self.cost_cny_estimate is not None:
            raise ValueError("incomplete USD cost cannot produce a CNY estimate")
        return self


class TraceFixRunner:
    """在独立 Git 克隆中装配并执行一个 TraceFix Agent。"""

    def __init__(self, llm_factory: LLMFactory | None = None) -> None:
        # 注入工厂让自动测试可以替换真实供应商，同时生产默认仍使用 LiteLLM。
        self._llm_factory = llm_factory or LiteLLMAdapter

    def run(self, config: RunConfig) -> RunResult:
        """执行任务并保证成功、预算终止或异常时都保存结构化结果。"""
        run_id = self._new_run_id()
        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()
        run_dir = config.output_dir.expanduser().resolve() / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        trace_path = run_dir / "trajectory.jsonl"
        diff_path = run_dir / "patch.diff"
        result_path = run_dir / "result.json"
        workspace_path = run_dir / "workspace"

        source_repo = str(config.repo.expanduser().resolve())
        source_commit: str | None = None
        workspace: Path | None = None
        state = AgentState(
            status=AgentStatus.FAILED,
            task=config.task,
            started_at=started_at,
            # 在模型调用前失败时费用确定为零；只有响应缺失费用字段时才标记不完整。
            cost_complete=True,
        )
        error: dict[str, JsonValue] | None = None
        changed_files: tuple[str, ...] = ()
        patch = ""
        sink: JSONLTraceSink | None = None
        agent: MinimalAgent | None = None
        llm_config = LLMConfig(
            model_name=config.model_name,
            temperature=0.0,
            max_output_tokens=config.per_request_output_tokens,
            timeout_seconds=config.llm_timeout_seconds,
            max_retries=config.llm_max_retries,
            extra_kwargs={},
        )
        provenance = collect_run_provenance(config.task, llm_config)

        try:
            sink = JSONLTraceSink(trace_path)
            load_environment_file(config.env_file)
            llm_config = llm_config.model_copy(
                update={"extra_kwargs": self._provider_kwargs(config.model_name)}
            )
            model_parameters = sanitize_payload(llm_config.model_dump(mode="json"))
            assert isinstance(model_parameters, dict)
            # Git 状态必须反映运行开始前的 TraceFix，而不能被刚创建的轨迹文件污染。
            provenance = provenance.model_copy(
                update={"model_parameters": model_parameters}
            )
            sink.write(
                TraceEvent(
                    event_type=TraceEventType.RUN_PROVENANCE,
                    task_id=run_id,
                    step=0,
                    payload={"provenance": provenance.model_dump(mode="json")},
                )
            )
            self._validate_credentials(config.model_name)
            source, source_commit = self._validate_source_repository(config.repo)
            source_repo = str(source)
            workspace = self._clone_repository(source, workspace_path)

            llm = self._llm_factory(llm_config)
            tools = create_default_tool_registry(
                workspace,
                test_timeout_seconds=min(120.0, float(config.agent_config.wall_time_seconds)),
            )
            agent = MinimalAgent(
                llm,
                tools,
                config=config.agent_config.model_copy(deep=True),
                trace_sink=sink,
            )
            state = agent.run(config.task)
        except KeyboardInterrupt:
            state = agent.state if agent is not None else state
            state.status = AgentStatus.INTERRUPTED
            state.stop_reason = "keyboard_interrupt"
            state.finished_at = datetime.now(UTC)
            error = {"type": "KeyboardInterrupt", "message": "run interrupted by user"}
        except Exception as exc:
            state = agent.state if agent is not None else state
            if state.status not in {AgentStatus.FAILED, AgentStatus.INTERRUPTED}:
                state.status = AgentStatus.FAILED
            state.stop_reason = getattr(exc, "code", "unexpected_run_error")
            state.finished_at = state.finished_at or datetime.now(UTC)
            error = self._serialize_error(exc)
            self._write_runner_error(sink, run_id, error)
        finally:
            if workspace is not None:
                try:
                    patch, changed_files = self._collect_diff(workspace)
                except Exception as exc:
                    # Diff 收集失败不应覆盖更早的根因，但必须在结果中可见。
                    if error is None:
                        error = self._serialize_error(exc)
                        state.status = AgentStatus.FAILED
                        state.stop_reason = getattr(exc, "code", "diff_collection_error")
            diff_path.write_text(patch, encoding="utf-8")
            if sink is not None:
                try:
                    sink.close()
                except TraceProtocolError as exc:
                    if error is None:
                        error = self._serialize_error(exc)
                        state.status = AgentStatus.FAILED
                        state.stop_reason = exc.code

        finished_at = state.finished_at or datetime.now(UTC)
        state.finished_at = finished_at
        cost_cny = (
            round(state.cost_usd * config.usd_cny_rate, 8) if state.cost_complete else None
        )
        result = RunResult(
            run_id=run_id,
            source_repo=source_repo,
            source_commit=source_commit,
            workspace=str(workspace) if workspace is not None else None,
            model_name=config.model_name,
            status=state.status,
            stop_reason=state.stop_reason,
            final_output=state.final_output,
            step_count=state.step_count,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            test_runs=state.test_runs,
            cost_usd=state.cost_usd,
            cost_complete=state.cost_complete,
            usd_cny_rate=config.usd_cny_rate,
            cost_cny_estimate=cost_cny,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=max(0.0, time.monotonic() - started_monotonic),
            changed_files=changed_files,
            trace_path=str(trace_path),
            diff_path=str(diff_path),
            result_path=str(result_path),
            agent_config=config.agent_config.model_copy(deep=True),
            context_metrics=state.context_metrics.model_copy(deep=True),
            provenance=provenance,
            error=error,
        )
        result_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result

    @staticmethod
    def _new_run_id() -> str:
        """生成便于按时间排序且几乎不会冲突的运行标识。"""
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{timestamp}-{uuid4().hex[:8]}"

    @staticmethod
    def _validate_credentials(model_name: str) -> None:
        """仅检查已明确支持的供应商密钥，不把密钥放入配置对象。"""
        if model_name.casefold().startswith("deepseek/") and not os.getenv(
            "DEEPSEEK_API_KEY"
        ):
            raise RunConfigurationError(
                "DEEPSEEK_API_KEY is required for DeepSeek models",
                context={"model_name": model_name, "env_var": "DEEPSEEK_API_KEY"},
            )

    @staticmethod
    def _provider_kwargs(model_name: str) -> dict[str, JsonValue]:
        """构造供应商专用参数，并遵循 OpenAI 兼容客户端的透传约定。"""
        if not model_name.casefold().startswith("deepseek/"):
            return {}

        # DeepSeek 官方要求 OpenAI Python 客户端把 thinking 放入 extra_body。
        # 若直接作为 LiteLLM 顶层参数传入，旧版适配器会在发起请求前误判为不支持。
        api_base = os.getenv("DEEPSEEK_API_BASE", DEFAULT_DEEPSEEK_API_BASE).rstrip("/")
        return {
            "api_base": api_base,
            "extra_body": {"thinking": {"type": "disabled"}},
        }

    @classmethod
    def _validate_source_repository(cls, value: Path) -> tuple[Path, str]:
        """定位 Git 根目录并拒绝会破坏复现性的未提交改动。"""
        requested = value.expanduser().resolve()
        if not requested.is_dir():
            raise WorkspaceError(
                "source repository must be an existing directory",
                context={"repo": str(requested)},
            )
        root_result = cls._run_git(
            ["rev-parse", "--show-toplevel"], cwd=requested, purpose="locate repository"
        )
        root = Path(root_result.stdout.strip()).resolve()
        # Runner 要求调用方明确传入仓库根目录。若静默接受仓库内任意子目录，
        # 临时测试目录可能意外继承外层仓库，进而绕过“不是仓库”的校验。
        if root != requested:
            raise WorkspaceError(
                "source repository path must be the Git repository root",
                context={"requested": str(requested), "repository_root": str(root)},
            )
        commit = cls._run_git(
            ["rev-parse", "--verify", "HEAD"], cwd=root, purpose="read source commit"
        ).stdout.strip()
        status = cls._run_git(
            ["status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            purpose="check source cleanliness",
        )
        if status.stdout.strip():
            raise WorkspaceError(
                "source repository must be clean before an isolated run",
                context={"repo": str(root), "status": status.stdout.splitlines()[:20]},
            )
        return root, commit

    @classmethod
    def _clone_repository(cls, source: Path, destination: Path) -> Path:
        """从已验证 HEAD 建立无硬链接的本地克隆，并保留完整 Git 元数据。"""
        destination.parent.mkdir(parents=True, exist_ok=True)
        cls._run_git(
            ["clone", "--quiet", "--no-hardlinks", str(source), str(destination)],
            cwd=destination.parent,
            purpose="clone isolated workspace",
        )
        return destination.resolve()

    @staticmethod
    def _run_git(
        arguments: list[str], *, cwd: Path, purpose: str
    ) -> subprocess.CompletedProcess[str]:
        """以参数数组执行 Git，并统一映射启动失败和非零退出码。"""
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkspaceError(
                f"cannot {purpose}: {exc}", context={"cwd": str(cwd)}
            ) from exc
        if result.returncode != 0:
            raise WorkspaceError(
                f"cannot {purpose}",
                context={
                    "cwd": str(cwd),
                    "returncode": result.returncode,
                    "stderr": result.stderr.strip(),
                },
            )
        return result

    @staticmethod
    def _collect_diff(workspace: Path) -> tuple[str, tuple[str, ...]]:
        """复用公开 diff 工具收集 tracked 与 untracked 修改。"""
        tool = GetGitDiffTool(workspace, max_output_chars=10_000_000)
        result = tool.execute(ToolCall(id="runner-final-diff", name=tool.spec.name))
        if not result.success or not isinstance(result.output, dict):
            raise WorkspaceError(
                "cannot collect final git diff",
                context={"tool_error": result.error, "output": result.output},
            )
        diff = result.output.get("diff", "")
        files = result.output.get("changed_files", [])
        if not isinstance(diff, str) or not isinstance(files, list):
            raise WorkspaceError("get_git_diff returned an invalid result shape")
        return diff, tuple(str(item) for item in files)

    @staticmethod
    def _serialize_error(exc: Exception) -> dict[str, JsonValue]:
        """把异常转换成脱敏 JSON，优先保留 TraceFix 稳定错误码。"""
        if isinstance(exc, TraceFixError):
            payload: Any = exc.to_dict()
        else:
            payload = {
                "type": type(exc).__name__,
                "code": "unexpected_run_error",
                "message": str(exc),
            }
        sanitized = sanitize_payload(payload)
        return sanitized if isinstance(sanitized, dict) else {"message": str(sanitized)}

    @staticmethod
    def _write_runner_error(
        sink: JSONLTraceSink | None,
        run_id: str,
        error: dict[str, JsonValue],
    ) -> None:
        """在 Agent 尚未启动时也尽量留下可诊断的轨迹事件。"""
        if sink is None or sink.closed:
            return
        try:
            sink.write(
                TraceEvent(
                    event_type=TraceEventType.ERROR,
                    task_id=run_id,
                    step=0,
                    payload={"error": error},
                )
            )
        except TraceProtocolError:
            # 原始异常比补写轨迹失败更有诊断价值，这里不覆盖它。
            return
