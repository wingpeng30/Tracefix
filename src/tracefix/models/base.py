"""与模型供应商无关的语言模型接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from tracefix.messages import Message, MessageRole
from tracefix.tools.base import ToolSpec


class LLMConfig(BaseModel):
    """一次模型适配器使用的稳定配置。"""

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
    """模型调用产生的输入、输出 Token 和美元成本。"""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class LLMResponse(BaseModel):
    """供应商响应经过规范化后的 TraceFix 响应。"""

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
    """供单 Agent 控制循环使用的同步 LLM 协议。"""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    @abstractmethod
    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
    ) -> LLMResponse:
        """根据完整对话和可用工具生成一条 assistant 响应。"""
