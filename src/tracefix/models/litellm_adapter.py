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
from tracefix.models.gateway import LLMGateway, PreparedRequest, canonical_json, sha256_bytes
from tracefix.models.input_bounds import (
    V41_RECIPE_VERSION,
    V41_TOKENIZER_SHA256,
    InputBound,
    count_deepseek_v41_request,
)
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
        self.gateway = LLMGateway()

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
        return self.complete_prepared(self.prepare_request(messages, tools))

    def prepare_request(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec] = ()
    ) -> PreparedRequest:
        kwargs = self.request_kwargs(messages, tools)
        # Preparing is the final point at which a changed body can be counted
        # before send. A prior context-window or budget check hits this same
        # cache entry; a changed body or tool schema is measured again here.
        measurement = self._measure_request(kwargs)
        counter_identity = measurement.counter_identity
        return self.gateway.freeze(kwargs, counter_identity=counter_identity)

    def complete_prepared(self, request: PreparedRequest) -> LLMResponse:
        kwargs = self.gateway.validate(request)
        client = self.client

        try:
            response = client.completion(**kwargs)
        except Exception as exc:
            raise self._map_provider_error(client, exc) from exc

        raw_response = self._dump_response(response)
        try:
            usage = self._parse_usage(client, response)
        except Exception as exc:
            partial_usage: dict[str, int] = {}
            provider_usage = _get(response, "usage")
            for source, target in (
                ("prompt_tokens", "input_tokens"),
                ("completion_tokens", "output_tokens"),
            ):
                value = _get(provider_usage, source)
                if type(value) is int and value >= 0:
                    partial_usage[target] = value
            raise LLMResponseFormatError(
                f"invalid LiteLLM usage data: {exc}",
                context={"usage": partial_usage, "raw_response": raw_response},
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

    def close(self) -> None:
        self.gateway.close()

    def request_kwargs(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec] = ()
    ) -> dict[str, Any]:
        """构造实际供应商调用参数，供调用和预算计数共用。"""
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
        return kwargs

    def count_input_tokens(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec] = ()
    ) -> int:
        """Return an auditable upper bound for the exact serialized request.

        LiteLLM silently falls back to an OpenAI tokenizer for unknown model
        names.  That estimate is useful for display, but it must not guard a
        hard DeepSeek budget.  A UTF-8 byte count is conservative for the
        byte-based tokenizer family and includes the tool schema verbatim.
        The fixed allowance covers provider-added chat framing.
        """
        kwargs = self.request_kwargs(messages, tools)
        value = self._measure_request(kwargs).conservative_input_tokens
        if value is None:
            raise LLMProviderError("provider adapter exposes no auditable token counter")
        return value

    def count_input_bound(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec] = ()
    ) -> InputBound:
        """Count the exact input once per wire body and bind the result to this request."""
        kwargs = self.request_kwargs(messages, tools)
        measured = self._measure_request(kwargs)
        return InputBound(
            measured.input_bound.tokens,
            measured.input_bound.status,
            measured.input_bound.method,
            measured.input_bound.identity,
            sha256_bytes(canonical_json(kwargs)),
        )

    def _counter_identity(self, kwargs: dict[str, Any]) -> str:
        if self.config.model_name.casefold() == "deepseek/deepseek-flash":
            return sha256_bytes(canonical_json({
                "kind": "deepseek-v41",
                "model": kwargs.get("model"),
                "tokenizer": V41_TOKENIZER_SHA256,
                "recipe": V41_RECIPE_VERSION,
                "api_base": kwargs.get("api_base"),
            }))
        counter = getattr(self.client, "token_counter", None)
        counter_type = type(counter)
        target = getattr(counter, "__func__", counter)
        return sha256_bytes(canonical_json({
            "kind": "provider-token-counter-v1",
            "model": self.config.model_name,
            "api_base": kwargs.get("api_base"),
            "counter_module": getattr(counter_type, "__module__", "unknown"),
            "counter_name": getattr(counter_type, "__qualname__", "unknown"),
            "callable_module": getattr(target, "__module__", "unknown"),
            "callable_name": getattr(target, "__qualname__", "unknown"),
        }))

    def _measure_request(self, kwargs: dict[str, Any]):
        identity = self._counter_identity(kwargs)

        def compute() -> tuple[InputBound, int | None]:
            bound = self._count_input_bound_for_kwargs(kwargs)
            try:
                conservative = self._count_input_tokens_for_kwargs(kwargs)
            except LLMProviderError:
                # Ordinary unbudgeted calls may still use a provider without a
                # local tokenizer. Budgeted paths call count_input_tokens and
                # fail closed; P2 separately requires a qualified bound.
                conservative = None
            return bound, conservative

        return self.gateway.measure(
            kwargs,
            counter_identity=identity,
            compute=compute,
        )

    def _count_input_tokens_for_kwargs(self, kwargs: dict[str, Any]) -> int:
        if self.config.model_name.casefold().startswith("deepseek/"):
            payload = {
                "model": kwargs["model"].split("/", 1)[-1],
                "messages": kwargs["messages"],
                "tools": kwargs.get("tools", []),
            }
            serialized = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            return len(serialized) + 1024
        counter = getattr(self.client, "token_counter", None)
        if counter is None:
            raise LLMProviderError("provider adapter exposes no auditable token counter")
        value = counter(
            model=kwargs["model"], messages=kwargs["messages"], tools=kwargs.get("tools")
        )
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise LLMProviderError("provider token counter returned an invalid input bound")
        return value

    def _count_input_bound_for_kwargs(self, kwargs: dict[str, Any]) -> InputBound:
        """Qualified model count for new formal requests; legacy byte counts stay diagnostic."""
        if self.config.model_name.casefold() == "deepseek/deepseek-flash":
            allowed = {
                "model", "messages", "tools", "timeout", "num_retries", "temperature",
                "max_tokens", "api_base", "extra_body",
            }
            if set(kwargs) - allowed or kwargs.get("extra_body") != {
                "thinking": {"type": "disabled"}
            }:
                return InputBound(
                    None, "unavailable", "deepseek-v41", "unsupported request parameters"
                )
            bound = count_deepseek_v41_request({
                "model": "deepseek-flash",
                "messages": kwargs["messages"],
                "tools": kwargs.get("tools", []),
                "thinking": kwargs.get("extra_body", {}).get("thinking"),
            })
            return InputBound(
                bound.tokens, bound.status, bound.method, bound.identity, None
            )
        return InputBound(None, "unavailable", "unknown", "no qualified model counter")

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
        usage = _get(response, "usage")
        if usage is None:
            raise ValueError("provider usage is missing")
        values: dict[str, int] = {}
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = _get(usage, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"provider usage {name} is missing or invalid")
            values[name] = value
        input_tokens = values["prompt_tokens"]
        output_tokens = values["completion_tokens"]
        total_tokens = values["total_tokens"]
        if total_tokens < input_tokens + output_tokens:
            raise ValueError("provider total tokens are smaller than input plus output")
        cost = self._calculate_cost(
            client,
            response,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
        )

    def _calculate_cost(
        self,
        client: Any,
        response: Any,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> float | None:
        """优先读取响应费用，失败时仍使用 LiteLLM 自带价格表估算。"""
        direct = getattr(client, "completion_cost", None)
        if callable(direct):
            try:
                return float(direct(completion_response=response))
            except Exception:
                # 新模型可能已能请求，但旧版 completion_cost 还不能识别响应中的模型别名。
                pass

        calculator = getattr(getattr(client, "cost_calculator", None), "completion_cost", None)
        if callable(calculator):
            try:
                return float(calculator(response, model=self.config.model_name))
            except Exception:
                pass

        per_token = getattr(client, "cost_per_token", None)
        if callable(per_token):
            try:
                input_cost, output_cost = per_token(
                    model=self.config.model_name,
                    prompt_tokens=input_tokens,
                    completion_tokens=output_tokens,
                    # LiteLLM 可从原始 usage 中识别缓存命中 Token，避免按普通输入价高估。
                    usage_object=_get(response, "usage"),
                )
                return float(input_cost) + float(output_cost)
            except Exception:
                pass
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
