"""与模型供应商无关的对话消息和强校验历史。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

from tracefix.exceptions import MessageProtocolError


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MessageRole(StrEnum):
    """模型对话支持的四种标准消息角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    """模型请求执行的一次结构化工具调用。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class Message(BaseModel):
    """带有时间、元数据和工具关联信息的标准消息。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    role: MessageRole
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    timestamp: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_role_shape(self) -> Message:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must include timezone information")

        if self.role is MessageRole.TOOL:
            if not self.tool_call_id:
                raise ValueError("tool messages require tool_call_id")
            if self.tool_calls:
                raise ValueError("tool messages cannot contain tool_calls")
            if self.content is None:
                raise ValueError("tool messages require content; use an empty string if needed")
            return self

        if self.tool_call_id is not None:
            raise ValueError("only tool messages may set tool_call_id")

        if self.role is not MessageRole.ASSISTANT and self.tool_calls:
            raise ValueError("only assistant messages may contain tool_calls")

        if self.content is None and not self.tool_calls:
            raise ValueError("a message must contain content or tool_calls")

        call_ids = [call.id for call in self.tool_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("tool call IDs must be unique within a message")
        return self


_MESSAGE_LIST_ADAPTER = TypeAdapter(list[Message])


class MessageHistory:
    """有序消息历史，并跨消息校验工具调用与结果的对应关系。"""

    def __init__(self, messages: Iterable[Message] = ()) -> None:
        self._messages: list[Message] = []
        self._known_call_ids: set[str] = set()
        self._pending_call_ids: set[str] = set()
        self.extend(messages)

    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self) -> Iterator[Message]:
        return iter(self.snapshot())

    def _append_validated(self, message: Message) -> None:
        stored = message.model_copy(deep=True)

        if stored.role is MessageRole.TOOL:
            call_id = stored.tool_call_id
            if call_id not in self._known_call_ids:
                raise MessageProtocolError(
                    f"tool message references unknown call ID: {call_id}",
                    context={"tool_call_id": call_id},
                )
            if call_id not in self._pending_call_ids:
                raise MessageProtocolError(
                    f"tool call already has a result: {call_id}",
                    context={"tool_call_id": call_id},
                )
            self._pending_call_ids.remove(call_id)
        else:
            if self._pending_call_ids:
                raise MessageProtocolError(
                    "all pending tool calls must receive results before the next non-tool message",
                    context={"pending_tool_call_ids": sorted(self._pending_call_ids)},
                )
            for call in stored.tool_calls:
                if call.id in self._known_call_ids:
                    raise MessageProtocolError(
                        f"tool call ID is already present in history: {call.id}",
                        context={"tool_call_id": call.id},
                    )
                self._known_call_ids.add(call.id)
                self._pending_call_ids.add(call.id)

        self._messages.append(stored)

    def append(self, message: Message) -> None:
        self.extend((message,))

    def extend(self, messages: Iterable[Message]) -> None:
        """原子地追加多条消息；任何校验失败都不会改变原历史。"""
        candidates = [message.model_copy(deep=True) for message in messages]
        staged = object.__new__(MessageHistory)
        staged._messages = [message.model_copy(deep=True) for message in self._messages]
        staged._known_call_ids = set(self._known_call_ids)
        staged._pending_call_ids = set(self._pending_call_ids)
        for message in candidates:
            staged._append_validated(message)
        self._messages = staged._messages
        self._known_call_ids = staged._known_call_ids
        self._pending_call_ids = staged._pending_call_ids

    def snapshot(self) -> tuple[Message, ...]:
        return tuple(message.model_copy(deep=True) for message in self._messages)

    @property
    def pending_tool_call_ids(self) -> frozenset[str]:
        return frozenset(self._pending_call_ids)

    def clear(self) -> None:
        self._messages.clear()
        self._known_call_ids.clear()
        self._pending_call_ids.clear()

    def to_json(self, *, indent: int | None = None) -> str:
        payload = [message.model_dump(mode="json") for message in self._messages]
        return json.dumps(payload, ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, value: str | bytes) -> MessageHistory:
        return cls(_MESSAGE_LIST_ADAPTER.validate_json(value))
