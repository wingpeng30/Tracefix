"""Agent 配置、运行状态与生命周期协议。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracefix.context import ContextConfig, ContextManager, ContextMetrics
from tracefix.messages import MessageHistory
from tracefix.models.base import BaseLLM
from tracefix.tools.base import ToolRegistry
from tracefix.tracing.base import TraceSink

DEFAULT_SYSTEM_PROMPT = """你是 TraceFix，一个负责修复 Python 仓库问题的 Coding Agent。
请先理解问题，再使用 search_code 和 read_file 定位相关实现，不要猜测文件内容。
使用 apply_patch 提交尽量小且聚焦根因的补丁；除非任务明确要求，否则不要修改、删除或跳过测试。
修改后应使用 run_tests 运行最相关的测试，并使用 get_git_diff 检查最终改动。
如果工具失败，请根据结构化错误调整方案，不要机械重复相同调用。
只有确认实现和测试结果后，才返回不包含工具调用的最终说明；说明应概括修改和测试结果。"""


class AgentStatus(StrEnum):
    """Agent 对外可见的生命周期状态。"""

    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class AgentConfig(BaseModel):
    """单次 Agent 运行使用的提示词和资源预算。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    system_prompt: str = Field(default=DEFAULT_SYSTEM_PROMPT, min_length=1)
    max_steps: int = Field(default=30, ge=1)
    max_input_tokens: int = Field(default=80_000, ge=1)
    max_output_tokens: int = Field(default=20_000, ge=1)
    wall_time_seconds: int = Field(default=1_200, ge=1)
    max_test_runs: int = Field(default=8, ge=1)
    context: ContextConfig = Field(default_factory=ContextConfig)


class AgentState(BaseModel):
    """一次任务运行过程中持续累计的可序列化状态。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    status: AgentStatus = AgentStatus.CREATED
    task: str | None = None
    step_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    cost_complete: bool = True
    test_runs: int = Field(default=0, ge=0)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stop_reason: str | None = None
    final_output: str | None = None
    context_metrics: ContextMetrics = Field(default_factory=ContextMetrics)

    @model_validator(mode="after")
    def validate_timestamps(self) -> AgentState:
        for field_name in ("started_at", "finished_at"):
            value = getattr(self, field_name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{field_name} must include timezone information")
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot be earlier than started_at")
        return self


class BaseAgent(ABC):
    """持有 Agent 依赖的基类，具体控制循环由子类实现。"""

    def __init__(
        self,
        llm: BaseLLM,
        tools: ToolRegistry | None = None,
        config: AgentConfig | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools or ToolRegistry()
        self.config = config or AgentConfig()
        self.trace_sink = trace_sink
        self.history = MessageHistory()
        self.state = AgentState()
        self.context_manager = ContextManager(self.config.context.model_copy(deep=True))

    def reset(self) -> None:
        """重置单次任务状态，同时保留模型、工具和追踪器依赖。"""
        self.history = MessageHistory()
        self.state = AgentState()
        self.context_manager = ContextManager(self.config.context.model_copy(deep=True))

    @abstractmethod
    def run(self, task: str) -> AgentState:
        """运行一个任务，直到进入终止状态。"""

    @abstractmethod
    def step(self) -> None:
        """执行一次模型请求以及该响应包含的工具调用。"""
