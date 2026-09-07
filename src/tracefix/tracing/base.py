"""Provider-neutral trace events and output sink protocol."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class TraceEventType(StrEnum):
    TASK_STARTED = "task_started"
    MESSAGE_ADDED = "message_added"
    MODEL_REQUESTED = "model_requested"
    MODEL_RESPONDED = "model_responded"
    TOOL_CALLED = "tool_called"
    TOOL_RETURNED = "tool_returned"
    AGENT_STATE_CHANGED = "agent_state_changed"
    ERROR = "error"
    TASK_FINISHED = "task_finished"


class TraceEvent(BaseModel):
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
    def write(self, event: TraceEvent) -> None:
        """Persist or forward one event."""

    def close(self) -> None:
        """Release sink resources."""

