from types import SimpleNamespace

import pytest

from tracefix import (
    LiteLLMAdapter,
    LLMAuthenticationError,
    LLMConfig,
    LLMResponseFormatError,
    Message,
    MessageRole,
    ToolSpec,
)


class AuthenticationError(Exception):
    pass


class RateLimitError(Exception):
    pass


class Timeout(Exception):
    pass


class ContextWindowExceededError(Exception):
    pass


class FakeLiteLLM:
    exceptions = SimpleNamespace(
        AuthenticationError=AuthenticationError,
        RateLimitError=RateLimitError,
        Timeout=Timeout,
        ContextWindowExceededError=ContextWindowExceededError,
    )

    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls = []

    def completion(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response

    @staticmethod
    def completion_cost(*, completion_response) -> float:
        return 0.0125


def make_response(*, content="done", tool_calls=None, raw_extra=None):
    raw = {
        "model": "provider/model-v1",
        "choices": [
            {
                "message": {"content": content, "tool_calls": tool_calls or []},
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
        **(raw_extra or {}),
    }
    return raw


def test_adapter_normalizes_text_response_and_request() -> None:
    client = FakeLiteLLM(make_response())
    adapter = LiteLLMAdapter(
        LLMConfig(model_name="provider/model", max_output_tokens=200, max_retries=1), client=client
    )

    result = adapter.complete([Message(role=MessageRole.USER, content="hello")])

    assert result.message.content == "done"
    assert result.usage.input_tokens == 12
    assert result.usage.cost_usd == 0.0125
    assert result.model_name == "provider/model-v1"
    assert client.calls[0]["max_tokens"] == 200
    assert client.calls[0]["num_retries"] == 1


def test_adapter_normalizes_multiple_tool_calls_and_tool_specs() -> None:
    calls = [
        {
            "id": "call-1",
            "function": {"name": "search_code", "arguments": '{"query": "Parser"}'},
        },
        {
            "id": "call-2",
            "function": {"name": "read_file", "arguments": {"path": "src/parser.py"}},
        },
    ]
    client = FakeLiteLLM(make_response(content=None, tool_calls=calls))
    adapter = LiteLLMAdapter(LLMConfig(model_name="provider/model"), client=client)
    spec = ToolSpec(name="search_code", description="search")

    result = adapter.complete([Message(role=MessageRole.USER, content="find parser")], [spec])

    assert [call.name for call in result.message.tool_calls] == ["search_code", "read_file"]
    assert result.message.tool_calls[0].arguments == {"query": "Parser"}
    assert client.calls[0]["tools"][0]["function"]["name"] == "search_code"


def test_format_error_preserves_usage_and_redacted_raw_response() -> None:
    malformed = [
        {"id": "call-1", "function": {"name": "search_code", "arguments": "not json"}}
    ]
    client = FakeLiteLLM(
        make_response(content=None, tool_calls=malformed, raw_extra={"api_key": "must-not-leak"})
    )
    adapter = LiteLLMAdapter(LLMConfig(model_name="provider/model"), client=client)

    with pytest.raises(LLMResponseFormatError) as captured:
        adapter.complete([Message(role=MessageRole.USER, content="find")])

    assert captured.value.context["usage"]["total_tokens"] == 16
    assert captured.value.context["raw_response"]["api_key"] == "<redacted>"


def test_malformed_usage_is_a_structured_format_error() -> None:
    response = make_response()
    response["usage"]["prompt_tokens"] = "not-a-number"
    adapter = LiteLLMAdapter(
        LLMConfig(model_name="provider/model"), client=FakeLiteLLM(response)
    )

    with pytest.raises(LLMResponseFormatError) as captured:
        adapter.complete([Message(role=MessageRole.USER, content="hello")])

    assert captured.value.context["usage"] == {}
    assert captured.value.context["raw_response"]["usage"]["prompt_tokens"] == "not-a-number"


def test_provider_exception_is_mapped_and_chained() -> None:
    provider_error = AuthenticationError("invalid key")
    adapter = LiteLLMAdapter(
        LLMConfig(model_name="provider/model"),
        client=FakeLiteLLM(error=provider_error),
    )

    with pytest.raises(LLMAuthenticationError) as captured:
        adapter.complete([Message(role=MessageRole.USER, content="hello")])

    assert captured.value.__cause__ is provider_error
    assert captured.value.context["provider_error"] == "AuthenticationError"


def test_model_config_rejects_reserved_overrides() -> None:
    with pytest.raises(ValueError):
        LLMConfig(model_name="provider/model", extra_kwargs={"model": "other"})

