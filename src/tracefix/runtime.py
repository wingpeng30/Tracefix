"""TraceFix 单任务运行编排、隔离工作区和结果模型。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from tracefix.agent import (
    AgentConfig,
    AgentState,
    AgentStatus,
    MinimalAgent,
    ToolPresentationMetrics,
)
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
from tracefix.provenance import (
    RunProvenance,
    collect_run_provenance,
    inspect_test_environment,
)
from tracefix.real_recipes import EnvironmentRecipe
from tracefix.repository import RepoMap, RepositoryIndexer
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
    test_python_executable: Path | None = None
    test_pythonpath_entries: tuple[Path, ...] = ()
    environment_recipe: EnvironmentRecipe | None = None
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
    agent_validation_status: Literal["unverified", "verified"] = "unverified"
    final_output: str | None = None
    step_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    test_runs: int = Field(default=0, ge=0)
    search_calls: int = Field(default=0, ge=0)
    file_read_calls: int = Field(default=0, ge=0)
    cached_tool_calls: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    cost_complete: bool
    usd_cny_rate: float = Field(gt=0)
    cost_cny_estimate: float | None = Field(default=None, ge=0)
    started_at: datetime
    finished_at: datetime
    duration_seconds: float = Field(ge=0)
    model_request_seconds: float = Field(default=0.0, ge=0)
    tool_execution_seconds: float = Field(default=0.0, ge=0)
    context_preparation_seconds: float = Field(default=0.0, ge=0)
    repository_index_seconds: float = Field(default=0.0, ge=0)
    changed_files: tuple[str, ...] = ()
    trace_path: str
    diff_path: str
    result_path: str
    agent_config: AgentConfig
    context_metrics: ContextMetrics = Field(default_factory=ContextMetrics)
    presentation_metrics: ToolPresentationMetrics = Field(default_factory=ToolPresentationMetrics)
    repo_map: RepoMap | None = None
    repo_map_path: str | None = None
    provenance: RunProvenance
    workspace_preparation: dict[str, JsonValue] = Field(default_factory=dict)
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
        repo_map_path = run_dir / "repo-map.json"

        source_repo = str(config.repo.expanduser().resolve())
        source_commit: str | None = None
        workspace: Path | None = None
        workspace_preparation: dict[str, JsonValue] = {}
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
        repository_map: RepoMap | None = None
        repository_index_seconds = 0.0
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
                update={
                    "model_parameters": model_parameters,
                    "test_environment": inspect_test_environment(
                        config.test_python_executable or sys.executable,
                        pythonpath_entries=config.test_pythonpath_entries,
                    ),
                }
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
            workspace_preparation = self._prepare_workspace(config, workspace, run_dir)
            if workspace_preparation.get("success") is not True:
                raise WorkspaceError(
                    "agent workspace preparation failed",
                    context={"workspace_preparation": workspace_preparation},
                )
            sink.write(
                TraceEvent(
                    event_type=TraceEventType.WORKSPACE_PREPARED,
                    task_id=run_id,
                    step=0,
                    payload=workspace_preparation,
                )
            )

            if config.agent_config.repo_map.enabled:
                # 索引失败不应被静默吞掉：它会导致模型少看到本应稳定提供的定位信息。
                # 但 AST 解析失败的单个文件由 RepositoryIndexer 自己作为 skipped 记录。
                index_started = time.monotonic()
                indexer = RepositoryIndexer(workspace, config.agent_config.repo_map)
                index = indexer.build()
                repository_map = indexer.make_repo_map(index, config.task)
                repository_index_seconds = time.monotonic() - index_started
                repo_map_path.write_text(repository_map.model_dump_json(indent=2), encoding="utf-8")
                sink.write(
                    TraceEvent(
                        event_type=TraceEventType.REPOSITORY_INDEXED,
                        task_id=run_id,
                        step=0,
                        payload={
                            "indexed_file_count": repository_map.indexed_file_count,
                            "symbol_count": repository_map.symbol_count,
                            "skipped_file_count": repository_map.skipped_file_count,
                            "candidate_files": list(repository_map.candidate_files),
                            "related_tests": list(repository_map.related_tests),
                            "duration_ms": round(repository_index_seconds * 1000, 3),
                        },
                    )
                )

            llm = self._llm_factory(llm_config)
            tools = create_default_tool_registry(
                workspace,
                test_timeout_seconds=min(120.0, float(config.agent_config.wall_time_seconds)),
                test_python_executable=config.test_python_executable,
                test_pythonpath_entries=config.test_pythonpath_entries,
                pytest_config=(
                    config.environment_recipe.pytest_config
                    if config.environment_recipe is not None
                    else None
                ),
            )
            agent = MinimalAgent(
                llm,
                tools,
                config=config.agent_config.model_copy(deep=True),
                trace_sink=sink,
                repository_map=repository_map.text if repository_map else None,
                repository_candidates=(repository_map.candidate_files if repository_map else ()),
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
        cost_cny = round(state.cost_usd * config.usd_cny_rate, 8) if state.cost_complete else None
        result = RunResult(
            run_id=run_id,
            source_repo=source_repo,
            source_commit=source_commit,
            workspace=str(workspace) if workspace is not None else None,
            model_name=config.model_name,
            status=state.status,
            stop_reason=state.stop_reason,
            agent_validation_status=state.validation_status,
            final_output=state.final_output,
            step_count=state.step_count,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            test_runs=state.test_runs,
            search_calls=state.search_calls,
            file_read_calls=state.file_read_calls,
            cached_tool_calls=state.cached_tool_calls,
            cost_usd=state.cost_usd,
            cost_complete=state.cost_complete,
            usd_cny_rate=config.usd_cny_rate,
            cost_cny_estimate=cost_cny,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=max(0.0, time.monotonic() - started_monotonic),
            model_request_seconds=state.model_request_seconds,
            tool_execution_seconds=state.tool_execution_seconds,
            context_preparation_seconds=state.context_preparation_seconds,
            repository_index_seconds=repository_index_seconds,
            changed_files=changed_files,
            trace_path=str(trace_path),
            diff_path=str(diff_path),
            result_path=str(result_path),
            agent_config=config.agent_config.model_copy(deep=True),
            context_metrics=state.context_metrics.model_copy(deep=True),
            presentation_metrics=state.presentation_metrics.model_copy(deep=True),
            repo_map=repository_map,
            repo_map_path=str(repo_map_path) if repository_map is not None else None,
            provenance=provenance,
            workspace_preparation=workspace_preparation,
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
        if model_name.casefold().startswith("deepseek/") and not os.getenv("DEEPSEEK_API_KEY"):
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

    @staticmethod
    def _prepare_workspace(
        config: RunConfig, workspace: Path, run_dir: Path
    ) -> dict[str, JsonValue]:
        """Apply only the frozen task build recipe and prove its source import."""
        recipe = config.environment_recipe
        python = str((config.test_python_executable or Path(sys.executable)).resolve())
        build_root = workspace / ".tracefix-build-tmp"
        build_root.mkdir(parents=True, exist_ok=True)
        exclude = workspace / ".git" / "info" / "exclude"
        if exclude.is_file():
            current = exclude.read_text(encoding="utf-8", errors="replace")
            for entry in (".tracefix-build-tmp/", ".tracefix-test-tmp/"):
                if entry not in current.splitlines():
                    with exclude.open("a", encoding="utf-8") as stream:
                        if current and not current.endswith("\n"):
                            stream.write("\n")
                        stream.write(f"{entry}\n")
                    current += f"{entry}\n"
        environment = dict(os.environ)
        for name in tuple(environment):
            normalized = name.upper()
            if any(
                marker in normalized
                for marker in ("API_KEY", "ACCESS_TOKEN", "PASSWORD", "SECRET", "CREDENTIAL")
            ):
                environment.pop(name, None)
        environment["TMP"] = str(build_root)
        environment["TEMP"] = str(build_root)
        environment["PIP_CACHE_DIR"] = str(build_root / "pip-cache")
        environment["PIP_NO_CACHE_DIR"] = "1"
        environment.pop("PYTEST_ADDOPTS", None)
        environment.pop("PYTEST_PLUGINS", None)
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        import_roots = [str(workspace / "src"), str(workspace)]
        import_roots.extend(str(path.resolve()) for path in config.test_pythonpath_entries)
        environment["PYTHONPATH"] = os.pathsep.join(import_roots)

        steps: list[dict[str, JsonValue]] = []
        for index, template in enumerate(recipe.build_commands if recipe else ()):
            command = [python if value == "{python}" else value for value in template]
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    command,
                    cwd=workspace,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=300,
                    check=False,
                    shell=False,
                )
                returncode = completed.returncode
                stdout, stderr = completed.stdout, completed.stderr
            except (OSError, subprocess.SubprocessError) as exc:
                returncode = None
                stdout, stderr = "", str(exc)
            stdout_path = run_dir / f"workspace-build-{index:02d}.stdout.txt"
            stderr_path = run_dir / f"workspace-build-{index:02d}.stderr.txt"
            stdout_path.write_text(stdout, encoding="utf-8")
            stderr_path.write_text(stderr, encoding="utf-8")
            steps.append(
                {
                    "command": command,
                    "returncode": returncode,
                    "duration_seconds": max(0.0, time.monotonic() - started),
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
                    "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
                }
            )
            if returncode != 0:
                return {
                    "success": False,
                    "recipe_fingerprint": recipe.fingerprint if recipe else None,
                    "build_steps": steps,
                    "source_import_probe": None,
                    "failure": "recipe build command failed",
                }

        probe: dict[str, JsonValue] | None = None
        if recipe is not None and recipe.source_import_probe:
            probe_name = recipe.source_import_probe
            roots = [str(workspace / "src"), str(workspace)]
            roots.extend(str(path.resolve()) for path in config.test_pythonpath_entries)
            probe_environment = dict(environment)
            probe_environment["PYTHONPATH"] = os.pathsep.join(roots)
            probe_environment["TRACEFIX_IMPORT_PROBE"] = probe_name
            script = (
                "import importlib, json, os, sys; "
                "name=os.environ['TRACEFIX_IMPORT_PROBE']; "
                "importlib.import_module(name); "
                "print(json.dumps({n:getattr(m,'__file__',None) for n,m in sys.modules.items() "
                "if n==name or n.startswith(name+'.')}))"
            )
            try:
                completed = subprocess.run(
                    [python, "-c", script],
                    cwd=workspace,
                    env=probe_environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    check=False,
                    shell=False,
                )
                probe_lines = completed.stdout.strip().splitlines()
                imported = (
                    json.loads(probe_lines[-1]) if completed.returncode == 0 and probe_lines else {}
                )
                workspace_resolved = workspace.resolve()
                paths = [
                    Path(path).resolve() for path in imported.values() if isinstance(path, str)
                ]
                valid = bool(paths) and all(
                    path.is_relative_to(workspace_resolved) for path in paths
                )
                probe = {
                    "module": probe_name,
                    "returncode": completed.returncode,
                    "paths": {str(key): value for key, value in imported.items()},
                    "valid": valid,
                    "stderr": completed.stderr[-4000:],
                }
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError) as exc:
                probe = {"module": probe_name, "valid": False, "error": str(exc)}
            if probe.get("valid") is not True:
                return {
                    "success": False,
                    "recipe_fingerprint": recipe.fingerprint,
                    "build_steps": steps,
                    "source_import_probe": probe,
                    "failure": "source import probe did not resolve to the agent checkout",
                }

        return {
            "success": True,
            "recipe_fingerprint": recipe.fingerprint if recipe else None,
            "build_steps": steps,
            "source_import_probe": probe,
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
        """从已验证 HEAD 建立隔离源码副本，并保留完整 Git 元数据。"""
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            cls._run_git(
                ["clone", "--quiet", "--no-hardlinks", str(source), str(destination)],
                cwd=destination.parent,
                purpose="clone isolated workspace",
            )
        except WorkspaceError as clone_error:
            # Git for Windows may start MSYS sh.exe for a local file clone;
            # restricted Windows workers can deny its signal-pipe setup. A
            # detached worktree gives the run its own files and index while
            # retaining the verified HEAD, without invoking upload-pack.
            try:
                cls._run_git(
                    ["worktree", "add", "--detach", str(destination), "HEAD"],
                    cwd=source,
                    purpose="create isolated workspace worktree",
                )
            except WorkspaceError as worktree_error:
                raise WorkspaceError(
                    "cannot create isolated workspace by clone or worktree",
                    context={
                        "cwd": str(destination.parent),
                        "clone_error": clone_error.context,
                        "worktree_error": worktree_error.context,
                    },
                ) from worktree_error
        return destination.resolve()

    @staticmethod
    def _run_git(
        arguments: list[str], *, cwd: Path, purpose: str
    ) -> subprocess.CompletedProcess[str]:
        """以参数数组执行 Git，并统一映射启动失败和非零退出码。"""
        try:
            result = subprocess.run(
                # run 目录会嵌套批次、任务和独立 workspace；Windows CI 下可能
                # 超过传统 MAX_PATH。命令级配置不会污染用户全局 Git 设置。
                ["git", "-c", "core.longpaths=true", *arguments],
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
            raise WorkspaceError(f"cannot {purpose}: {exc}", context={"cwd": str(cwd)}) from exc
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
