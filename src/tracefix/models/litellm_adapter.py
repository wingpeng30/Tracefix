"""基于 LiteLLM 实现的 TraceFix 模型适配层。"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from tracefix.exceptions import (
    LLMAuthenticationError,
    LLMContextWindowError,
    LLMError,
    LLMProviderError,
    LLMRateLimitError,
    LLMResponseFormatError,
    LLMTimeoutError,
    sanitize_payload,
)
from tracefix.messages import Message, MessageRole, ToolCall
from tracefix.models.base import BaseLLM, LLMConfig, LLMResponse, TokenUsage
from tracefix.tools.base import ToolSpec


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


class LiteLLMAdapter(BaseLLM):
    """把 LiteLLM Chat Completion 规范化为 TraceFix 强类型对象。"""

    def __init__(self, config: LLMConfig, *, client: Any | None = None) -> None:
        super().__init__(config)
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                self._client = importlib.import_module("litellm")
            except ModuleNotFoundError as exc:
                raise LLMProviderError(
                    "LiteLLM is not installed; install TraceFix with the 'llm' extra",
                    context={"install_hint": "pip install -e .[llm]"},
                ) from exc
        return self._client

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
    ) -> LLMResponse:
        client = self.client
        kwargs: dict[str, Any] = {
            "model": self.config.model_name,
            "messages": [self._format_message(message) for message in messages],
            "timeout": self.config.timeout_seconds,
            "num_retries": self.config.max_retries,
            **self.config.extra_kwargs,
        }
        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature
        if self.config.max_output_tokens is not None:
            kwargs["max_tokens"] = self.config.max_output_tokens
        if tools:
            kwargs["tools"] = [tool.to_openai_tool() for tool in tools]

        try:
            response = client.completion(**kwargs)
        except Exception as exc:
            raise self._map_provider_error(client, exc) from exc

        raw_response = self._dump_response(response)
        try:
            usage = self._parse_usage(client, response)
        except Exception as exc:
            raise LLMResponseFormatError(
                f"invalid LiteLLM usage data: {exc}",
                context={"usage": {}, "raw_response": raw_response},
            ) from exc

        try:
            choices = _get(response, "choices", []) or []
            if not choices:
                raise ValueError("provider response contains no choices")
            choice = choices[0]
            provider_message = _get(choice, "message")
            if provider_message is None:
                raise ValueError("provider choice contains no message")

            tool_calls = tuple(
                self._parse_tool_call(tool_call)
                for tool_call in (_get(provider_message, "tool_calls", []) or [])
            )
            message = Message(
                role=MessageRole.ASSISTANT,
                content=_get(provider_message, "content"),
                tool_calls=tool_calls,
                metadata={"provider": "litellm"},
            )
        except LLMError:
            raise
        except Exception as exc:
            raise LLMResponseFormatError(
                f"invalid LiteLLM response: {exc}",
                context={"usage": usage.model_dump(mode="json"), "raw_response": raw_response},
            ) from exc

        return LLMResponse(
            message=message,
            usage=usage,
            model_name=str(
                _get(response, "model", self.config.model_name) or self.config.model_name
            ),
            finish_reason=_get(choice, "finish_reason"),
            raw_response=raw_response,
        )

    @staticmethod
    def _format_message(message: Message) -> dict[str, Any]:
        formatted: dict[str, Any] = {"role": message.role.value, "content": message.content}
        if message.tool_calls:
            formatted["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id is not None:
            formatted["tool_call_id"] = message.tool_call_id
        return formatted

    @staticmethod
    def _parse_tool_call(value: Any) -> ToolCall:
        function = _get(value, "function")
        if function is None:
            raise ValueError("tool call contains no function")
        arguments = _get(function, "arguments", {})
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, Mapping):
            raise ValueError("tool call arguments must decode to an object")
        return ToolCall(
            id=str(_get(value, "id", "")),
            name=str(_get(function, "name", "")),
            arguments=dict(arguments),
        )

    def _parse_usage(self, client: Any, response: Any) -> TokenUsage:
        usage = _get(response, "usage", {}) or {}
        input_tokens = int(_get(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(_get(usage, "completion_tokens", 0) or 0)
        total_tokens = int(_get(usage, "total_tokens", input_tokens + output_tokens) or 0)
        cost = self._calculate_cost(client, response)
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
        )

    def _calculate_cost(self, client: Any, response: Any) -> float | None:
        try:
            direct = getattr(client, "completion_cost", None)
            if callable(direct):
                return float(direct(completion_response=response))
            calculator = getattr(getattr(client, "cost_calculator", None), "completion_cost", None)
            if callable(calculator):
                return float(calculator(response, model=self.config.model_name))
        except Exception:
            return None
        return None

    @staticmethod
    def _dump_response(response: Any) -> dict[str, Any]:
        try:
            if isinstance(response, Mapping):
                raw: Any = dict(response)
            elif callable(getattr(response, "model_dump", None)):
                try:
                    raw = response.model_dump(mode="json")
                except TypeError:
                    raw = response.model_dump()
            else:
                raw = {"repr": repr(response)}
        except Exception:
            raw = {"repr": repr(response)}
        sanitized = sanitize_payload(raw)
        return sanitized if isinstance(sanitized, dict) else {"value": sanitized}

    @staticmethod
    def _map_provider_error(client: Any, error: Exception) -> LLMError:
        exceptions = getattr(client, "exceptions", None)
        mappings = (
            ("AuthenticationError", LLMAuthenticationError),
            ("RateLimitError", LLMRateLimitError),
            ("Timeout", LLMTimeoutError),
            ("APITimeoutError", LLMTimeoutError),
            ("ContextWindowExceededError", LLMContextWindowError),
        )
        for provider_name, tracefix_type in mappings:
            provider_type = getattr(exceptions, provider_name, None)
            if isinstance(provider_type, type) and isinstance(error, provider_type):
                return tracefix_type(str(error), context={"provider_error": provider_name})
        return LLMProviderError(
            str(error),
            context={"provider_error": error.__class__.__name__},
        )
