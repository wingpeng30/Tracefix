"""TraceFix 单任务运行编排、隔离工作区和结果模型。"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
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
from tracefix.checkpoint import CheckpointError, CheckpointStore, ProcessLock
from tracefix.context import ContextMetrics
from tracefix.docker_backend import DockerToolBackend
from tracefix.exceptions import (
    RunConfigurationError,
    TraceFixError,
    TraceProtocolError,
    WorkspaceError,
    sanitize_payload,
)
from tracefix.mcp_serena import SerenaMCP
from tracefix.messages import Message, MessageHistory, ToolCall
from tracefix.models import BaseLLM, LiteLLMAdapter, LLMConfig
from tracefix.provenance import (
    RunProvenance,
    collect_run_provenance,
    inspect_test_environment,
)
from tracefix.real_recipes import EnvironmentRecipe
from tracefix.repository import RepoMap, RepositoryIndexer
from tracefix.tools import GetGitDiffTool, create_default_tool_registry
from tracefix.tools.builtin import RunTestsTool
from tracefix.tracing import JSONLTraceSink, TraceEvent, TraceEventType

DEFAULT_MODEL_NAME = "deepseek/deepseek-flash"
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


def default_output_root() -> Path:
    """Allow the user to move new Agent run artifacts off the source drive."""
    configured = os.environ.get("TRACEFIX_RUNS_ROOT")
    if configured:
        return Path(configured)
    return Path("runs")


def config_identity_sha256(
    config: RunConfig, manifest_config: dict[str, Any] | None = None,
) -> str:
    """Hash effective configuration, preserving identity for manifests predating gate targets."""
    legacy_empty_targets = (
        manifest_config is not None
        and "regression_targets" not in manifest_config
        and not config.regression_targets
    )
    exclude = {"regression_targets"} if legacy_empty_targets else None
    return hashlib.sha256(
        config.model_dump_json(exclude=exclude).encode("utf-8")
    ).hexdigest()


class RunConfig(BaseModel):
    """运行一个真实 TraceFix 任务需要的完整、可序列化配置。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    repo: Path
    task: str = Field(min_length=1)
    model_name: str = Field(default=DEFAULT_MODEL_NAME, min_length=1)
    output_dir: Path = Field(default_factory=default_output_root)
    env_file: Path | None = Path(".env")
    usd_cny_rate: float = Field(default=DEFAULT_USD_CNY_RATE, gt=0)
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    per_request_output_tokens: int = Field(default=4_096, ge=1)
    test_python_executable: Path | None = None
    test_pythonpath_entries: tuple[Path, ...] = ()
    test_target: str | None = None
    regression_targets: tuple[str, ...] = ()
    source_import: str | None = None
    skills_root: Path | None = None
    mcp_serena_image_id: str | None = None
    environment_recipe: EnvironmentRecipe | None = None
    test_environment_variables: dict[str, str] = Field(default_factory=dict)
    execution_backend: Literal["local", "docker"] = "local"
    docker_task_id: str | None = None
    docker_input_root: Path | None = None
    docker_profile: Literal["frozen", "synthetic", "ordinary"] = "frozen"
    docker_image_id: str | None = None
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
        if self.source_import is not None and not all(
            segment.isidentifier() for segment in self.source_import.split(".")
        ):
            raise ValueError("source_import must be a dotted Python module name")
        if self.test_target is not None:
            target_path = Path(self.test_target.split("::", 1)[0])
            if (
                not str(target_path).strip()
                or target_path.is_absolute()
                or ".." in target_path.parts
                or "\n" in self.test_target
                or "\r" in self.test_target
            ):
                raise ValueError("test_target must be a relative pytest path or node ID")
        if len(set(self.regression_targets)) != len(self.regression_targets):
            raise ValueError("regression_targets must not contain duplicates")
        if self.test_target in self.regression_targets:
            raise ValueError("regression_targets must be distinct from test_target")
        if self.regression_targets and (
            self.execution_backend != "local" or not self.test_target or not self.source_import
        ):
            raise ValueError(
                "regression_targets require a local backend, test target, and source import"
            )
        for target in self.regression_targets:
            target_path = Path(target.split("::", 1)[0])
            if (
                not target_path.parts or target_path.is_absolute()
                or ".." in target_path.parts or "\n" in target or "\r" in target
                or target.startswith("-") or "\x00" in target
            ):
                raise ValueError("regression target must be a relative pytest path or node ID")
        if self.execution_backend == "docker" and self.docker_profile != "ordinary" and (
            not self.docker_task_id or not self.docker_input_root
        ):
            raise ValueError("Docker execution requires a task ID and input root")
        if self.execution_backend == "docker" and self.docker_profile == "ordinary":
            if not self.docker_image_id or not self.test_target or not self.source_import:
                raise ValueError(
                    "ordinary Docker requires an immutable image ID, test target, and source import"
                )
            if self.docker_task_id not in (None, "tracefix-ordinary"):
                raise ValueError("ordinary Docker does not accept a frozen task ID")
            if self.docker_input_root is not None:
                raise ValueError("ordinary Docker does not accept frozen task inputs")
        if self.execution_backend == "docker" and self.skills_root is not None:
            raise ValueError("custom Skills directories are supported only by local execution")
        if self.execution_backend == "docker" and self.mcp_serena_image_id is not None:
            raise ValueError("Serena MCP is supported only with the local Agent backend")
        if self.execution_backend != "docker" and (
            self.docker_profile != "frozen" or self.docker_image_id is not None
        ):
            raise ValueError("Docker profile and image identity require the Docker backend")
        if self.docker_profile == "synthetic" and not self.docker_image_id:
            raise ValueError("synthetic Docker execution requires a frozen image ID")
        if self.docker_profile == "frozen" and self.docker_image_id is not None:
            raise ValueError("frozen pilot image identities cannot be overridden")
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
    validation_gate_status: Literal["passed", "failed", "incomplete"] | None = None
    validation_gate_results: list[dict[str, JsonValue]] = Field(default_factory=list)
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
    usage_complete: bool = True
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
    patch_sha256: str | None = None
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

    @classmethod
    def inspect(cls, run_dir: Path) -> dict[str, Any]:
        """Read a local session without constructing a model or modifying its checkout."""
        root = run_dir.expanduser().resolve()
        manifest_path = root / "session.json"
        if not manifest_path.is_file():
            return {"resumable": False, "reasons": ["session manifest is missing"]}
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            config = RunConfig.model_validate(manifest["config"])
            recorded = manifest["identity"]
            actual = dict(recorded)
            actual["config_sha256"] = config_identity_sha256(config, manifest["config"])
            actual["implementation_sha256"] = cls._implementation_sha256()
            actual["test_environment_sha256"] = inspect_test_environment(
                config.test_python_executable or sys.executable,
                pythonpath_entries=config.test_pythonpath_entries,
            ).fingerprint_sha256
            reasons: list[str] = []
            if config.regression_targets:
                baseline_path = root / "validation-baseline.json"
                if not baseline_path.is_file():
                    reasons.append("regression validation baseline is missing")
                elif hashlib.sha256(baseline_path.read_bytes()).hexdigest() != recorded.get(
                    "validation_baseline_sha256"
                ):
                    reasons.append("regression validation baseline identity changed")
                else:
                    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
                    if (
                        baseline.get("complete") is not True
                        or [row.get("target") for row in baseline.get("targets", [])]
                        != list(config.regression_targets)
                        or baseline.get("source_commit") != recorded.get("source_commit")
                        or baseline.get("test_environment_sha256")
                        != actual.get("test_environment_sha256")
                    ):
                        reasons.append("regression validation baseline does not match the run")
            source, commit = cls._validate_source_repository(config.repo)
            if (
                str(source) != str(config.repo.expanduser().resolve())
                or commit != recorded["source_commit"]
            ):
                reasons.append("source repository identity changed")
            workspace = root / "workspace"
            if not workspace.is_dir():
                reasons.append("original checkout is missing")
            else:
                head = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=workspace,
                    capture_output=True, text=True, check=False,
                )
                if head.returncode != 0 or head.stdout.strip() != recorded["source_commit"]:
                    reasons.append("checkout commit changed")
            repo_map_path = root / "repo-map.json"
            map_data = (
                json.loads(repo_map_path.read_text(encoding="utf-8"))
                if repo_map_path.is_file() else None
            )
            actual["repo_map_sha256"] = hashlib.sha256(
                (RepoMap.model_validate(map_data).text if map_data else "").encode("utf-8")
            ).hexdigest()
            store = CheckpointStore(root, recorded)
            inspection = store.inspect(current_identity=actual)
            reasons.extend(inspection.reasons)
            if inspection.resumable and workspace.is_dir():
                snapshot = store.load(current_identity=actual)
                protected = snapshot.payload.get("protected_dirs")
                if not isinstance(protected, list) or not all(
                    isinstance(name, str) for name in protected
                ) or not set(protected).issubset({
                    ".tracefix-build-tmp", ".tracefix-test-tmp"
                }):
                    reasons.append("checkpoint protected directory state is invalid")
                    protected = [".tracefix-build-tmp"]
                saved_agent = snapshot.payload.get("agent")
                if not isinstance(saved_agent, dict):
                    reasons.append("checkpoint agent state is invalid")
                else:
                    AgentState.model_validate(saved_agent["state"])
                    history = MessageHistory(
                        Message.model_validate(item) for item in saved_agent["history"]
                    )
                    if history.pending_tool_call_ids:
                        reasons.append("checkpoint message history has pending calls")
                    memory = saved_agent["memory"]
                    if not isinstance(memory, dict) or any(
                        name not in memory for name in MinimalAgent._RECOVERY_FIELDS
                    ):
                        reasons.append("checkpoint runtime memory is incomplete")
                patch, _ = cls._collect_diff(workspace, set(protected))
                if ".tracefix-test-tmp" in protected and (
                    not (workspace / ".tracefix-test-tmp").is_dir()
                    or (workspace / ".tracefix-test-tmp").is_symlink()
                ):
                    reasons.append("test temporary directory changed")
                registry = create_default_tool_registry(
                    workspace,
                    evidence_dir=root / "test-evidence",
                    protected_dirs=set(protected),
                    skills_enabled=config.agent_config.skills_enabled,
                    skill_limits=config.agent_config.skill_limits,
                    skills_root=config.skills_root,
                    test_timeout_seconds=min(120.0, float(config.agent_config.wall_time_seconds)),
                    test_python_executable=config.test_python_executable,
                    test_pythonpath_entries=config.test_pythonpath_entries,
                    test_environment_variables=config.test_environment_variables,
                )
                tool_sha = hashlib.sha256(json.dumps(
                    [spec.model_dump(mode="json") for spec in registry.specs()],
                    sort_keys=True, ensure_ascii=False,
                ).encode("utf-8")).hexdigest()
                if tool_sha != recorded["tool_sha256"]:
                    reasons.append("tool definitions changed")
                if config.agent_config.skills_enabled:
                    try:
                        registry.get("load_skill").restore_recovery_state(
                            snapshot.payload["skills"]
                        )
                    except TraceFixError:
                        reasons.append("skill contents changed")
                if hashlib.sha256(patch.encode("utf-8")).hexdigest() != snapshot.payload.get(
                    "workspace_diff_sha256"
                ):
                    reasons.append("checkout patch changed since checkpoint")
                fact = saved_agent["memory"].get("_last_test_evidence") if saved_agent else None
                if isinstance(fact, dict) and fact.get("valid") is True and (
                    fact.get("source_sha256") != snapshot.payload.get("workspace_diff_sha256")
                ):
                    reasons.append("passing test does not match checkpoint patch")
                trace = (root / "trajectory.jsonl").read_bytes()
                size = snapshot.payload.get("trace_size")
                if not isinstance(size, int) or size > len(trace) or size < 0:
                    reasons.append("trajectory prefix is missing")
                elif (
                    hashlib.sha256(trace[:size]).hexdigest()
                    != snapshot.payload.get("trace_sha256")
                ):
                    reasons.append("trajectory prefix changed")
                else:
                    for line in trace[size:].splitlines():
                        event = json.loads(line)
                        if event.get("event_type") not in {
                            TraceEventType.AGENT_STATE_CHANGED.value,
                            TraceEventType.TASK_FINISHED.value,
                        }:
                            reasons.append("uncommitted model or tool outcome after checkpoint")
                            break
                        if event.get("event_type") == TraceEventType.TASK_FINISHED.value:
                            terminal_state = event.get("payload", {}).get("state", {})
                            if terminal_state.get("status") != AgentStatus.INTERRUPTED.value:
                                reasons.append("run has a non-interrupted terminal event")
                if (root / "result.json").is_file():
                    result = RunResult.model_validate_json(
                        (root / "result.json").read_text(encoding="utf-8")
                    )
                    if result.status is not AgentStatus.INTERRUPTED:
                        reasons.append("run is not interrupted")
            return {
                "resumable": not reasons and inspection.resumable,
                "reasons": reasons,
                "sequence": inspection.sequence,
                "run": str(root),
                "step_count": (
                    snapshot.payload["agent"]["state"]["step_count"]
                    if inspection.resumable else None
                ),
                "task": (
                    snapshot.payload["agent"]["state"]["task"]
                    if inspection.resumable else None
                ),
                "phase": (
                    snapshot.payload["agent"]["state"]["phase"]
                    if inspection.resumable else None
                ),
                "validation_status": (
                    snapshot.payload["agent"]["state"]["validation_status"]
                    if inspection.resumable else None
                ),
                "last_test_evidence": (
                    snapshot.payload["agent"].get("memory", {}).get("_last_test_evidence")
                    if inspection.resumable else None
                ),
                "workspace_diff_sha256": (
                    snapshot.payload.get("workspace_diff_sha256")
                    if inspection.resumable else None
                ),
            }
        except (OSError, ValueError, KeyError, TypeError, TraceFixError) as exc:
            return {"resumable": False, "reasons": [str(exc)], "run": str(root)}

    def resume(self, run_dir: Path) -> RunResult:
        """Continue a validated local session in its original checkout."""
        root = run_dir.expanduser().resolve()
        manifest_path = root / "session.json"
        if not manifest_path.is_file():
            raise CheckpointError("session manifest is missing")
        with ProcessLock(root):
            inspection = self.inspect(root)
            if not inspection["resumable"]:
                raise CheckpointError(
                    "run cannot be resumed", context={"reasons": inspection["reasons"]}
                )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            config = RunConfig.model_validate(manifest["config"])
            if config.execution_backend != "local":
                raise CheckpointError("Docker sessions cannot be resumed")
            store = CheckpointStore(root, manifest["identity"])
            snapshot = store.load()
            workspace = root / "workspace"
            protected = snapshot.payload.get("protected_dirs")
            if not isinstance(protected, list) or not all(
                isinstance(name, str) for name in protected
            ) or not set(protected).issubset({
                ".tracefix-build-tmp", ".tracefix-test-tmp"
            }):
                raise CheckpointError("checkpoint protected directory state is invalid")
            protected_dirs = set(protected)
            if ".tracefix-test-tmp" in protected_dirs and not (
                workspace / ".tracefix-test-tmp"
            ).is_dir() or (workspace / ".tracefix-test-tmp").is_symlink():
                raise CheckpointError("test temporary directory changed since checkpoint")
            repo_map_path = root / "repo-map.json"
            repo_map = (
                RepoMap.model_validate_json(repo_map_path.read_text(encoding="utf-8"))
                if repo_map_path.is_file() else None
            )
            tools = create_default_tool_registry(
                workspace,
                evidence_dir=root / "test-evidence",
                protected_dirs=protected_dirs,
                skills_enabled=config.agent_config.skills_enabled,
                skill_limits=config.agent_config.skill_limits,
                skills_root=config.skills_root,
                test_timeout_seconds=min(120.0, float(config.agent_config.wall_time_seconds)),
                test_python_executable=config.test_python_executable,
                test_pythonpath_entries=config.test_pythonpath_entries,
                pytest_config=None,
                test_environment_variables=config.test_environment_variables,
            )
            mcp_manager = None
            if config.mcp_serena_image_id is not None:
                mcp_manager = SerenaMCP(workspace, root, config.mcp_serena_image_id)
                mcp_manager.preflight()
                for tool in mcp_manager.tools():
                    tools.register(tool)
            tool_hash = hashlib.sha256(json.dumps(
                [spec.model_dump(mode="json") for spec in tools.specs()],
                sort_keys=True, ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
            if tool_hash != manifest["identity"]["tool_sha256"]:
                raise CheckpointError("tool definitions changed since checkpoint")
            skill_tool = tools.get("load_skill") if config.agent_config.skills_enabled else None
            if skill_tool is not None:
                skill_tool.restore_recovery_state(snapshot.payload["skills"])
            load_environment_file(config.env_file)
            self._validate_credentials(config.model_name)
            llm_config = LLMConfig(
                model_name=config.model_name,
                temperature=0.0,
                max_output_tokens=config.per_request_output_tokens,
                timeout_seconds=config.llm_timeout_seconds,
                max_retries=config.llm_max_retries,
                extra_kwargs=self._provider_kwargs(config.model_name),
            )
            trace_path = root / "trajectory.jsonl"
            result_path = root / "result.json"
            old_result = result_path.read_bytes() if result_path.is_file() else None
            if old_result is not None:
                (root / f"result-before-resume-{snapshot.sequence}.json").write_bytes(old_result)
            sequence = snapshot.sequence
            with JSONLTraceSink(trace_path, append=True) as sink:
                sink.write(TraceEvent(
                    event_type=TraceEventType.SESSION_RESUMED,
                    task_id=root.name,
                    step=int(snapshot.payload["agent"]["state"]["step_count"]),
                    payload={"checkpoint_sequence": sequence},
                ))
                agent = MinimalAgent(
                    self._llm_factory(llm_config), tools,
                    config=config.agent_config.model_copy(deep=True),
                    trace_sink=sink,
                    repository_map=repo_map.text if repo_map else None,
                    repository_candidates=repo_map.candidate_files if repo_map else (),
                )

                if config.regression_targets:
                    baseline_path = root / "validation-baseline.json"
                    baseline_bytes = baseline_path.read_bytes()
                    if hashlib.sha256(baseline_bytes).hexdigest() != manifest["identity"].get(
                        "validation_baseline_sha256"
                    ):
                        raise CheckpointError("regression validation baseline identity changed")
                    baseline_data = json.loads(baseline_bytes.decode("utf-8"))
                    agent.validation_targets = config.regression_targets
                    agent.configured_original_target = config.test_target
                    agent.validation_baseline = {
                        row["target"]: row["status"] for row in baseline_data["targets"]
                    }

                    def resumed_checkout_identity() -> tuple[str, bool]:
                        current_patch, _ = self._collect_diff(workspace, protected_dirs)
                        return (
                            hashlib.sha256(current_patch.encode("utf-8")).hexdigest(),
                            bool(current_patch.strip()),
                        )

                    agent.validation_source_identity = resumed_checkout_identity

                def save_checkpoint(active: MinimalAgent) -> None:
                    nonlocal sequence
                    patch, _ = self._collect_diff(workspace, protected_dirs)
                    patch_sha = hashlib.sha256(patch.encode("utf-8")).hexdigest()
                    active.verify_test_source(patch_sha)
                    trace = trace_path.read_bytes()
                    sequence += 1
                    store.save({
                        "agent": active.checkpoint_payload(),
                        "workspace_diff_sha256": patch_sha,
                        "trace_size": len(trace),
                        "trace_sha256": hashlib.sha256(trace).hexdigest(),
                        "skills": skill_tool.recovery_state() if skill_tool else None,
                        "protected_dirs": sorted(protected_dirs),
                    }, sequence=sequence)

                agent.checkpoint_callback = save_checkpoint
                try:
                    state = agent.resume(snapshot.payload["agent"])
                finally:
                    if mcp_manager is not None:
                        mcp_manager.close()
            patch, changed_files = self._collect_diff(workspace, protected_dirs)
            diff_path = root / "patch.diff"
            diff_path.write_text(patch, encoding="utf-8")
            provenance = collect_run_provenance(config.task, llm_config)
            result = RunResult(
                run_id=root.name,
                source_repo=str(config.repo.expanduser().resolve()),
                source_commit=manifest["identity"]["source_commit"],
                workspace=str(workspace),
                model_name=config.model_name,
                status=state.status,
                stop_reason=state.stop_reason,
                agent_validation_status=state.validation_status,
                validation_gate_status=state.validation_gate_status,
                validation_gate_results=[dict(row) for row in state.validation_gate_results],
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
                usage_complete=state.usage_complete,
                usd_cny_rate=config.usd_cny_rate,
                cost_cny_estimate=(
                    round(state.cost_usd * config.usd_cny_rate, 8)
                    if state.cost_complete else None
                ),
                started_at=state.started_at or datetime.now(UTC),
                finished_at=state.finished_at or datetime.now(UTC),
                duration_seconds=max(0.0, time.monotonic() - agent._started_monotonic),
                model_request_seconds=state.model_request_seconds,
                tool_execution_seconds=state.tool_execution_seconds,
                context_preparation_seconds=state.context_preparation_seconds,
                changed_files=changed_files,
                trace_path=str(trace_path),
                diff_path=str(diff_path),
                patch_sha256=hashlib.sha256(diff_path.read_bytes()).hexdigest(),
                result_path=str(result_path),
                agent_config=config.agent_config.model_copy(deep=True),
                context_metrics=state.context_metrics.model_copy(deep=True),
                presentation_metrics=state.presentation_metrics.model_copy(deep=True),
                repo_map=repo_map,
                repo_map_path=str(repo_map_path) if repo_map else None,
                provenance=provenance,
                workspace_preparation=manifest["workspace_preparation"],
            )
            result_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
            return result

    def run(self, config: RunConfig) -> RunResult:
        """执行任务并保证成功、预算终止或异常时都保存结构化结果。"""
        source_path = config.repo.expanduser().resolve()
        output_root = config.output_dir.expanduser().resolve()
        if output_root.is_relative_to(source_path):
            raise RunConfigurationError(
                "output directory must be outside the source repository",
                context={"repo": str(source_path), "output_dir": str(output_root)},
            )
        run_id = self._new_run_id()
        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()
        run_dir = output_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        trace_path = run_dir / "trajectory.jsonl"
        diff_path = run_dir / "patch.diff"
        result_path = run_dir / "result.json"
        workspace_path = run_dir / "workspace"
        repo_map_path = run_dir / "repo-map.json"

        source_repo = str(config.repo.expanduser().resolve())
        source_commit: str | None = None
        workspace: Path | None = None
        docker_backend: DockerToolBackend | None = None
        mcp_manager: SerenaMCP | None = None
        tools = None
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
        internal_artifacts: set[str] = set()
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
                    "test_environment": (
                        None if config.docker_profile == "ordinary"
                        and config.execution_backend == "docker"
                        else inspect_test_environment(
                            config.test_python_executable or sys.executable,
                            pythonpath_entries=config.test_pythonpath_entries,
                        )
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
            if config.execution_backend == "docker":
                assert config.docker_profile == "ordinary" or (
                    config.docker_task_id and config.docker_input_root
                )
                docker_backend = DockerToolBackend(
                    task_id=config.docker_task_id or "tracefix-ordinary",
                    input_root=config.docker_input_root or run_dir,
                    run_dir=run_dir,
                    run_id=run_id,
                    timeout_seconds=min(120, int(config.agent_config.wall_time_seconds)),
                    profile=config.docker_profile,
                    image_id=config.docker_image_id,
                )
                docker_prepare_options = (
                    {"source_import_probe": config.source_import}
                    if config.docker_profile == "ordinary" else {}
                )
                tools = docker_backend.prepare(
                    source_commit,
                    source,
                    Path(__file__).resolve().parents[2],
                    repo_map_task=config.task,
                    repo_map_config=config.agent_config.repo_map,
                    skills_enabled=config.agent_config.skills_enabled,
                    skill_limits=config.agent_config.skill_limits,
                    **docker_prepare_options,
                )
                workspace_preparation = docker_backend.workspace_preparation
                if config.docker_profile == "ordinary":
                    workspace_preparation["test_target"] = config.test_target
                repository_map = docker_backend.repo_map
                if repository_map is not None:
                    repo_map_path.write_text(
                        repository_map.model_dump_json(indent=2), encoding="utf-8"
                    )
                    repository_index_seconds = float(
                        docker_backend.workspace_preparation.get("repo_map_seconds", 0.0)
                    )
                internal_artifacts.add(".tracefix-build-tmp")
            else:
                workspace = self._clone_repository(source, workspace_path)
                build_root = workspace / ".tracefix-build-tmp"
                if not build_root.exists() and not build_root.is_symlink():
                    internal_artifacts.add(build_root.name)
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

            if config.agent_config.repo_map.enabled and config.execution_backend == "local":
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

            if tools is None:
                assert workspace is not None
                tools = create_default_tool_registry(
                    workspace,
                    evidence_dir=run_dir / "test-evidence",
                    protected_dirs=internal_artifacts,
                    skills_enabled=config.agent_config.skills_enabled,
                    skill_limits=config.agent_config.skill_limits,
                    skills_root=config.skills_root,
                    test_timeout_seconds=min(120.0, float(config.agent_config.wall_time_seconds)),
                    test_python_executable=config.test_python_executable,
                    test_pythonpath_entries=config.test_pythonpath_entries,
                    pytest_config=(
                        config.environment_recipe.pytest_config
                        if config.environment_recipe is not None
                        else None
                    ),
                    test_environment_variables=config.test_environment_variables,
                )
            if config.mcp_serena_image_id is not None:
                assert workspace is not None
                mcp_manager = SerenaMCP(workspace, run_dir, config.mcp_serena_image_id)
                mcp_manager.preflight()
                for tool in mcp_manager.tools():
                    tools.register(tool)

            regression_baseline: dict[str, str] = {}
            regression_baseline_seconds = 0.0
            if config.regression_targets:
                if workspace is None or config.test_target is None:
                    raise RunConfigurationError(
                        "end-of-task regression validation requires local execution"
                    )
                required_processes = 2 * len(config.regression_targets) + 1
                if config.agent_config.max_test_runs < required_processes:
                    raise RunConfigurationError(
                        "test budget cannot cover regression baselines and final validation",
                        context={
                            "minimum_test_runs": required_processes,
                            "configured_test_runs": config.agent_config.max_test_runs,
                        },
                    )
                baseline_started = time.monotonic()
                regression_baseline = self._prepare_regression_baseline(
                    config, source, run_dir, sink, run_id
                )
                regression_baseline_seconds = time.monotonic() - baseline_started
            llm = self._llm_factory(llm_config)
            agent = MinimalAgent(
                llm,
                tools,
                config=config.agent_config.model_copy(deep=True),
                trace_sink=sink,
                repository_map=repository_map.text if repository_map else None,
                repository_candidates=(repository_map.candidate_files if repository_map else ()),
            )
            if config.regression_targets:
                assert workspace is not None
                agent.validation_targets = config.regression_targets
                agent.configured_original_target = config.test_target
                agent.validation_baseline = regression_baseline
                agent.validation_baseline_runs = len(config.regression_targets)
                agent.validation_baseline_seconds = regression_baseline_seconds

                def checkout_identity() -> tuple[str, bool]:
                    current_patch, _ = self._collect_diff(workspace, internal_artifacts)
                    return (
                        hashlib.sha256(current_patch.encode("utf-8")).hexdigest(),
                        bool(current_patch.strip()),
                    )

                agent.validation_source_identity = checkout_identity
            if config.execution_backend == "docker" and config.docker_profile == "ordinary":
                (run_dir / "session.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "checkpoint_supported": False,
                            "config": config.model_dump(mode="json"),
                            "identity": {
                                "source_commit": source_commit,
                                "config_sha256": config_identity_sha256(config),
                                "image_id": config.docker_image_id,
                            },
                            "workspace_preparation": workspace_preparation,
                        },
                        ensure_ascii=False, indent=2,
                    ), encoding="utf-8",
                )
            if (
                config.execution_backend == "local"
                and config.environment_recipe is None
                and not config.test_environment_variables
            ):
                checkpoint_identity = {
                    "source_commit": source_commit,
                    "config_sha256": config_identity_sha256(config),
                    "tool_sha256": hashlib.sha256(
                        json.dumps(
                            [spec.model_dump(mode="json") for spec in tools.specs()],
                            sort_keys=True, ensure_ascii=False,
                        ).encode("utf-8")
                    ).hexdigest(),
                    "repo_map_sha256": hashlib.sha256(
                        (repository_map.text if repository_map else "").encode("utf-8")
                    ).hexdigest(),
                    "implementation_sha256": self._implementation_sha256(),
                    "test_environment_sha256": inspect_test_environment(
                        config.test_python_executable or sys.executable,
                        pythonpath_entries=config.test_pythonpath_entries,
                    ).fingerprint_sha256,
                }
                if config.regression_targets:
                    baseline_path = run_dir / "validation-baseline.json"
                    checkpoint_identity["validation_baseline_sha256"] = hashlib.sha256(
                        baseline_path.read_bytes()
                    ).hexdigest()
                manifest_path = run_dir / "session.json"
                manifest_path.write_text(
                    json.dumps(
                        {"schema_version": 1, "config": config.model_dump(mode="json"),
                         "identity": checkpoint_identity,
                         "workspace_preparation": workspace_preparation},
                        ensure_ascii=False, indent=2,
                    ), encoding="utf-8",
                )
                store = CheckpointStore(run_dir, checkpoint_identity)
                sequence = 0

                def save_checkpoint(active: MinimalAgent) -> None:
                    nonlocal sequence
                    assert workspace is not None
                    current_diff, _ = self._collect_diff(workspace, internal_artifacts)
                    current_sha = hashlib.sha256(current_diff.encode("utf-8")).hexdigest()
                    active.verify_test_source(current_sha)
                    trace_bytes = trace_path.read_bytes()
                    skill_tool = (
                        tools.get("load_skill") if config.agent_config.skills_enabled else None
                    )
                    sequence += 1
                    store.save(
                        {
                            "agent": active.checkpoint_payload(),
                            "workspace_diff_sha256": current_sha,
                            "trace_size": len(trace_bytes),
                            "trace_sha256": hashlib.sha256(trace_bytes).hexdigest(),
                            "skills": skill_tool.recovery_state() if skill_tool else None,
                            "protected_dirs": sorted(internal_artifacts),
                        },
                        sequence=sequence,
                    )

                agent.checkpoint_callback = save_checkpoint
            if docker_backend is not None:
                docker_backend.set_phase("agent_running")
            agent_task = config.task
            if config.test_target:
                agent_task += (
                    f"\n\n指定公开测试：pytest -q {config.test_target}。"
                    "请先运行，修改后重跑并检查 Diff。"
                )
            if config.regression_targets:
                agent_task += (
                    "\n\n结束前强制验收目标（不可修改或删除）：\n"
                    + "\n".join(
                        f"- pytest -q {target}" for target in
                        (config.test_target, *config.regression_targets)
                    )
                    + "\n系统将在你提交最终答复前运行全部目标；失败时会提供结果并允许继续修复。"
                )
            state = agent.run(agent_task)
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
            if mcp_manager is not None:
                try:
                    mcp_manager.close()
                except Exception as exc:
                    if error is None:
                        error = self._serialize_error(exc)
                        state.status = AgentStatus.FAILED
                        state.stop_reason = "mcp_cleanup_error"
            if docker_backend is not None:
                try:
                    if docker_backend.session is None:
                        raise WorkspaceError("Docker tool session was not initialized")
                    remote_diff = docker_backend.session.call("get_git_diff", {"context_lines": 3})
                    if not remote_diff.success or not isinstance(remote_diff.output, dict):
                        raise WorkspaceError(
                            "cannot collect final container product diff",
                            context={"error": remote_diff.error},
                        )
                    if remote_diff.output.get("truncated"):
                        raise WorkspaceError("container product diff exceeds export limit")
                    patch = str(remote_diff.output.get("diff", ""))
                    changed_files = tuple(
                        str(item) for item in remote_diff.output.get("changed_files", [])
                    )
                    docker_backend.export_evidence()
                    identity_path = run_dir / "container-identity.json"
                    identity_path.write_text(
                        json.dumps(
                            docker_backend.workspace_preparation,
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                except Exception as exc:
                    if error is None:
                        error = self._serialize_error(exc)
                        state.status = AgentStatus.FAILED
                        state.stop_reason = getattr(exc, "code", "container_export_error")
                    try:
                        docker_backend.export_evidence()
                    except Exception:
                        pass
            elif workspace is not None:
                try:
                    patch, changed_files = self._collect_diff(workspace, internal_artifacts)
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
            workspace=(
                str(workspace)
                if workspace is not None
                else f"docker://{docker_backend.container_id}/work/agent"
                if docker_backend and docker_backend.container_id
                else None
            ),
            model_name=config.model_name,
            status=state.status,
            stop_reason=state.stop_reason,
            agent_validation_status=state.validation_status,
            validation_gate_status=state.validation_gate_status,
            validation_gate_results=[dict(row) for row in state.validation_gate_results],
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
            usage_complete=state.usage_complete,
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
            patch_sha256=hashlib.sha256(diff_path.read_bytes()).hexdigest(),
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
        # Do not advertise a terminal container phase until the run result is durable.
        if docker_backend is not None:
            try:
                docker_backend.set_phase(
                    "interrupted"
                    if state.status is AgentStatus.INTERRUPTED
                    else "failed"
                    if state.status is AgentStatus.FAILED or error is not None
                    else "completed"
                )
            except Exception:
                pass
        if docker_backend is not None:
            try:
                docker_backend.close(
                    remove=(
                        config.docker_profile == "ordinary"
                        or (
                            state.status is AgentStatus.COMPLETED
                            and error is None and bool(changed_files)
                        )
                    )
                )
            except Exception as exc:
                try:
                    docker_backend.set_phase("cleanup_failed")
                except Exception:
                    pass
                result = result.model_copy(update={
                    "status": AgentStatus.FAILED,
                    "stop_reason": "container_cleanup_failed",
                    "error": self._serialize_error(exc),
                })
                result_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result

    @staticmethod
    def _new_run_id() -> str:
        """生成便于按时间排序且几乎不会冲突的运行标识。"""
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{timestamp}-{uuid4().hex[:8]}"

    @staticmethod
    def _implementation_sha256() -> str:
        """Bind local checkpoints to the exact installed Python implementation."""
        root = Path(__file__).resolve().parent
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*.py")):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()

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

    @classmethod
    def _prepare_regression_baseline(
        cls, config: RunConfig, source: Path, run_dir: Path,
        sink: JSONLTraceSink, run_id: str,
    ) -> dict[str, str]:
        """Record each declared target against an isolated, frozen source checkout."""
        from tracefix.messages import ToolCall

        baseline_path = run_dir / "validation-baseline.json"
        if baseline_path.exists():
            raise RunConfigurationError("validation baseline already exists")
        original = config.test_target
        if original is None:
            raise RunConfigurationError("end-of-task regression validation requires test_target")
        for required in (original, *config.regression_targets):
            name = required.split("::", 1)[0]
            relative = Path(name)
            candidate = (source / relative).resolve()
            if (
                not name or relative.is_absolute() or ".." in relative.parts
                or required.startswith("-") or "\n" in required or "\r" in required
                or not candidate.is_relative_to(source.resolve()) or not candidate.is_file()
            ):
                raise RunConfigurationError(
                    "validation target does not exist as a safe relative path in the frozen source",
                    context={"target": required},
                )
            try:
                RunTestsTool._parse_command(f"pytest -q {shlex.quote(required)}")
            except Exception as exc:
                raise RunConfigurationError(
                    "validation target is not allowed by the pytest command policy",
                    context={"target": required, "reason": str(exc)},
                ) from exc
        rows: list[dict[str, Any]] = []
        evidence_root = run_dir / "validation-baseline-evidence"
        for index, target in enumerate(config.regression_targets):
            name = target.split("::", 1)[0]
            relative = Path(name)
            candidate = (source / relative).resolve()
            if not candidate.is_relative_to(source.resolve()) or not candidate.is_file():
                raise RunConfigurationError(
                    "regression target does not exist in the frozen source",
                    context={"target": target},
                )
            with tempfile.TemporaryDirectory(prefix="tracefix-baseline-") as temporary:
                root = Path(temporary)
                checkout = cls._clone_repository(source, root / "checkout")
                preparation = cls._prepare_workspace(config, checkout, root)
                if preparation.get("success") is not True:
                    raise WorkspaceError(
                        "regression baseline checkout preparation failed",
                        context={"target": target, "preparation": preparation},
                    )
                call = ToolCall(
                    id=uuid4().hex,
                    name="run_tests",
                    arguments={"command": f"pytest -q {shlex.quote(target)}"},
                )
                sink.write(TraceEvent(
                    event_type=TraceEventType.VALIDATION_BASELINE_STARTED,
                    task_id=run_id,
                    step=0,
                    payload={"target": target, "call_id": call.id, "origin": "validation_baseline"},
                ))
                result = RunTestsTool(
                    checkout,
                    default_timeout_seconds=min(
                        120.0, float(config.agent_config.wall_time_seconds)
                    ),
                    python_executable=config.test_python_executable,
                    pythonpath_entries=config.test_pythonpath_entries,
                    pytest_config=(
                        config.environment_recipe.pytest_config
                        if config.environment_recipe is not None else None
                    ),
                    environment_variables=config.test_environment_variables,
                    evidence_dir=evidence_root / str(index),
                ).execute(call).model_dump(mode="json")
            output = result.get("output") if isinstance(result.get("output"), dict) else {}
            if result.get("success") is True and output.get("test_status") == "passed":
                status = "passed"
            elif (
                output.get("test_status") == "test_failure"
                and output.get("returncode") == 1
                and (output.get("test_counts") or {}).get("failures", 0) > 0
                and (output.get("test_counts") or {}).get("errors", 0) == 0
            ):
                status = "failed"
            else:
                status = "incomplete"
            sink.write(TraceEvent(
                event_type=TraceEventType.VALIDATION_BASELINE_COMPLETED,
                task_id=run_id,
                step=0,
                payload={
                    "target": target, "call_id": result.get("call_id"),
                    "status": status, "origin": "validation_baseline",
                },
            ))
            if status == "incomplete":
                progress = {
                    "schema_version": 1,
                    "source_commit": cls._run_git(
                        ["rev-parse", "HEAD"], cwd=source, purpose="read baseline commit"
                    ).stdout.strip(),
                    "targets": [*rows, {"target": target, "status": status, "test": result}],
                    "complete": False,
                }
                (run_dir / "validation-baseline-progress.json").write_text(
                    json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                raise WorkspaceError(
                    "regression baseline could not be verified",
                    context={"target": target, "test": result,
                             "diagnostic_path": str(run_dir / "validation-baseline-progress.json")},
                )
            rows.append({"target": target, "status": status, "test": result})
            (run_dir / "validation-baseline-progress.json").write_text(
                json.dumps({"schema_version": 1, "targets": rows, "complete": False},
                           ensure_ascii=False, indent=2), encoding="utf-8"
            )
        payload = {
            "schema_version": 1,
            "source_commit": cls._run_git(
                ["rev-parse", "HEAD"], cwd=source, purpose="read baseline commit"
            ).stdout.strip(),
            "test_environment_sha256": inspect_test_environment(
                config.test_python_executable or sys.executable,
                pythonpath_entries=config.test_pythonpath_entries,
            ).fingerprint_sha256,
            "targets": rows,
            "complete": True,
        }
        baseline_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (run_dir / "validation-baseline-progress.json").unlink(missing_ok=True)
        return {row["target"]: row["status"] for row in rows}

    @staticmethod
    def _prepare_workspace(
        config: RunConfig, workspace: Path, run_dir: Path
    ) -> dict[str, JsonValue]:
        """Apply only the frozen task build recipe and prove its source import."""
        recipe = config.environment_recipe
        python = str((config.test_python_executable or Path(sys.executable)).resolve())
        build_root = workspace / ".tracefix-build-tmp"
        if build_root.exists() or build_root.is_symlink():
            raise WorkspaceError("TraceFix build temporary path already exists")
        build_root.mkdir(parents=True)
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
        if config.source_import or config.test_target:
            test_python = Path(python)
            if not test_python.is_file():
                raise WorkspaceError(f"test Python executable does not exist: {test_python}")
            try:
                pytest_check = subprocess.run(
                    [python, "-c", "import pytest"],
                    cwd=workspace,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise WorkspaceError(f"cannot check pytest: {exc}") from exc
            if pytest_check.returncode != 0:
                raise WorkspaceError("pytest is unavailable in the selected test Python")
        if config.test_target:
            target_path = Path(config.test_target.split("::", 1)[0])
            if (
                target_path.is_absolute()
                or ".." in target_path.parts
                or not (workspace / target_path).is_file()
            ):
                raise WorkspaceError("test target must be an existing file inside the checkout")
        if config.source_import or (recipe is not None and recipe.source_import_probe):
            probe_name = config.source_import or recipe.source_import_probe
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
                    "recipe_fingerprint": recipe.fingerprint if recipe else None,
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
        if destination.exists() or destination.is_symlink() or cls._is_junction(destination):
            raise WorkspaceError(
                "isolated workspace destination already exists",
                context={"destination": str(destination)},
            )
        try:
            cls._run_git(
                ["clone", "--quiet", "--no-hardlinks", str(source), str(destination)],
                cwd=destination.parent,
                purpose="clone isolated workspace",
            )
        except WorkspaceError as clone_error:
            cls._discard_partial_clone(destination)
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
    def _is_junction(path: Path) -> bool:
        """检测 Windows junction，同时兼容没有 Path.is_junction 的解释器。"""
        checker = getattr(path, "is_junction", None)
        return bool(checker and checker())

    @staticmethod
    def _discard_partial_clone(destination: Path) -> None:
        """只删除本次从空目标创建、且仍位于原父目录内的失败 clone。"""
        if destination.is_symlink() or TraceFixRunner._is_junction(destination):
            raise WorkspaceError(
                "refusing to remove a linked partial clone",
                context={"destination": str(destination)},
            )
        try:
            parent = destination.parent.resolve(strict=True)
            resolved = destination.resolve(strict=True)
        except FileNotFoundError:
            return
        if resolved.parent != parent or not resolved.is_dir():
            raise WorkspaceError(
                "refusing to remove an unexpected partial clone path",
                context={"destination": str(destination), "resolved": str(resolved)},
            )
        shutil.rmtree(resolved)

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
    def _collect_diff(
        workspace: Path, protected_dirs: set[str] | None = None
    ) -> tuple[str, tuple[str, ...]]:
        """复用公开 diff 工具收集 tracked 与 untracked 修改。"""
        tool = GetGitDiffTool(workspace, max_output_chars=10_000_000, protected_dirs=protected_dirs)
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
