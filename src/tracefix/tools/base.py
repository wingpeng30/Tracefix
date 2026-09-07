"""Provider-neutral tool descriptions, results, and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from tracefix.exceptions import ToolAlreadyRegisteredError, ToolNotFoundError
from tracefix.messages import ToolCall


class ReservedToolName(StrEnum):
    SEARCH_CODE = "search_code"
    READ_FILE = "read_file"
    APPLY_PATCH = "apply_patch"
    RUN_TESTS = "run_tests"
    GET_GIT_DIFF = "get_git_diff"


RESERVED_TOOL_NAMES = frozenset(member.value for member in ReservedToolName)


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    description: str = Field(min_length=1)
    input_schema: dict[str, JsonValue] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    @model_validator(mode="after")
    def require_object_schema(self) -> ToolSpec:
        if self.input_schema.get("type") != "object":
            raise ValueError("tool input_schema must have type='object'")
        return self

    def to_openai_tool(self) -> dict[str, JsonValue]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    success: bool
    output: JsonValue = None
    error: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_ms: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_error_state(self) -> ToolResult:
        if self.success and self.error is not None:
            raise ValueError("successful tool results cannot contain an error")
        if not self.success and not self.error:
            raise ValueError("failed tool results require an error message")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must include timezone information")
        return self


class BaseTool(ABC):
    @property
    @abstractmethod
    def spec(self) -> ToolSpec:
        """Return the model-visible tool definition."""

    @abstractmethod
    def execute(self, call: ToolCall) -> ToolResult:
        """Execute one validated call synchronously."""


class ToolRegistry:
    def __init__(self, tools: Iterable[BaseTool] = ()) -> None:
        self._tools: dict[str, BaseTool] = {}
        for tool in tools:
            self.register(tool)

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[BaseTool]:
        return iter(tuple(self._tools.values()))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def register(self, tool: BaseTool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ToolAlreadyRegisteredError(
                f"tool is already registered: {name}", context={"tool_name": name}
            )
        self._tools[name] = tool

    def get(self, name: str) -> BaseTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(
                f"tool is not registered: {name}",
                context={"tool_name": name, "available_tools": list(self._tools)},
            ) from exc

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(tool.spec.model_copy(deep=True) for tool in self._tools.values())

