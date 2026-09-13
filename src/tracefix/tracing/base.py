"""与输出介质无关的轨迹事件和接收器协议。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class TraceEventType(StrEnum):
    """Agent 生命周期中可观测的稳定事件类型。"""

    TASK_STARTED = "task_started"
    MESSAGE_ADDED = "message_added"
    CONTEXT_PREPARED = "context_prepared"
    CONTEXT_COMPACTED = "context_compacted"
    MODEL_REQUEST_VIEW = "model_request_view"
    MODEL_REQUESTED = "model_requested"
    RUN_PROVENANCE = "run_provenance"
    REPOSITORY_INDEXED = "repository_indexed"
    REPO_MAP_ADDED = "repo_map_added"
    MODEL_RESPONDED = "model_responded"
    TOOL_CALLED = "tool_called"
    TOOL_RETURNED = "tool_returned"
    TOOL_RESULT_PRESENTED = "tool_result_presented"
    AGENT_STATE_CHANGED = "agent_state_changed"
    AGENT_PHASE_CHANGED = "agent_phase_changed"
    ERROR = "error"
    TASK_FINISHED = "task_finished"


class TraceEvent(BaseModel):
    """可安全 JSON 序列化的一条 Agent 轨迹事件。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    event_type: TraceEventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    task_id: str | None = None
    step: int | None = Field(default=None, ge=0)
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_timezone(self) -> TraceEvent:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must include timezone information")
        return self


@runtime_checkable
class TraceSink(Protocol):
    """轨迹事件接收器需要实现的同步协议。"""

    def write(self, event: TraceEvent) -> None:
        """持久化或转发一条事件。"""

    def close(self) -> None:
        """释放接收器持有的资源。"""
