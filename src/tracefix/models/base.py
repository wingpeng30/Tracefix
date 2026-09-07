"""Provider-neutral language-model interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from tracefix.messages import Message, MessageRole
from tracefix.tools.base import ToolSpec


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    model_name: str = Field(min_length=1)
    temperature: float | None = Field(default=0.0, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=1)
    timeout_seconds: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    extra_kwargs: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_reserved_kwargs(self) -> LLMConfig:
        reserved = {"messages", "model", "tools"}
        overlap = reserved.intersection(self.extra_kwargs)
        if overlap:
            raise ValueError(f"extra_kwargs cannot override reserved fields: {sorted(overlap)}")
        return self


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class LLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: Message
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model_name: str = Field(min_length=1)
    finish_reason: str | None = None
    raw_response: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_assistant_message(self) -> LLMResponse:
        if self.message.role is not MessageRole.ASSISTANT:
            raise ValueError("LLM responses must contain an assistant message")
        return self


class BaseLLM(ABC):
    """Synchronous LLM contract used by a single-agent control loop."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    @abstractmethod
    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
    ) -> LLMResponse:
        """Generate one assistant response for the supplied conversation."""


