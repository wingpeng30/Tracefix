"""Agent configuration, state, and lifecycle contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracefix.messages import MessageHistory
from tracefix.models.base import BaseLLM
from tracefix.tools.base import ToolRegistry


class AgentStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    max_steps: int = Field(default=30, ge=1)
    max_input_tokens: int = Field(default=80_000, ge=1)
    max_output_tokens: int = Field(default=20_000, ge=1)
    wall_time_seconds: int = Field(default=1_200, ge=1)
    max_test_runs: int = Field(default=8, ge=1)


class AgentState(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    status: AgentStatus = AgentStatus.CREATED
    task: str | None = None
    step_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    test_runs: int = Field(default=0, ge=0)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stop_reason: str | None = None

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
    """Dependency-owning base class; subclasses define the control loop."""

    def __init__(
        self,
        llm: BaseLLM,
        tools: ToolRegistry | None = None,
        config: AgentConfig | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools or ToolRegistry()
        self.config = config or AgentConfig()
        self.history = MessageHistory()
        self.state = AgentState()

    def reset(self) -> None:
        """Reset task-local state while preserving configured dependencies."""
        self.history = MessageHistory()
        self.state = AgentState()

    @abstractmethod
    def run(self, task: str) -> AgentState:
        """Run a task to a terminal state."""

    @abstractmethod
    def step(self) -> None:
        """Perform one model/tool interaction step."""

