"""Stable exception hierarchy shared by TraceFix components."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
}


def sanitize_payload(value: Any) -> Any:
    """Return a JSON-friendly payload with common credential fields redacted."""
    if isinstance(value, Mapping):
        return {
            str(key): (
                "<redacted>"
                if str(key).lower() in _SENSITIVE_KEYS
                else sanitize_payload(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_payload(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


class TraceFixError(Exception):
    """Base exception carrying a stable error code and structured context."""

    code = "tracefix_error"

    def __init__(self, message: str = "", *, context: Mapping[str, Any] | None = None) -> None:
        self.message = message or self.__class__.__name__
        self.context = sanitize_payload(dict(context or {}))
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.__class__.__name__,
            "code": self.code,
            "message": self.message,
            "context": self.context,
        }


class AgentError(TraceFixError):
    code = "agent_error"


class AgentInterrupted(AgentError):
    """Intentional control-flow interruption, not necessarily a failure."""

    code = "agent_interrupted"


class AgentCompleted(AgentInterrupted):
    code = "agent_completed"


class AgentLimitExceeded(AgentInterrupted):
    code = "agent_limit_exceeded"


class StepLimitExceeded(AgentLimitExceeded):
    code = "step_limit_exceeded"


class TokenBudgetExceeded(AgentLimitExceeded):
    code = "token_budget_exceeded"


class TimeLimitExceeded(AgentLimitExceeded):
    code = "time_limit_exceeded"


class TestLimitExceeded(AgentLimitExceeded):
    code = "test_limit_exceeded"


class LLMError(TraceFixError):
    code = "llm_error"


class LLMAuthenticationError(LLMError):
    code = "llm_authentication_error"


class LLMRateLimitError(LLMError):
    code = "llm_rate_limit_error"


class LLMTimeoutError(LLMError):
    code = "llm_timeout_error"


class LLMContextWindowError(LLMError):
    code = "llm_context_window_error"


class LLMResponseFormatError(LLMError):
    code = "llm_response_format_error"


class LLMProviderError(LLMError):
    code = "llm_provider_error"


class ToolError(TraceFixError):
    code = "tool_error"


class ToolNotFoundError(ToolError):
    code = "tool_not_found"


class ToolAlreadyRegisteredError(ToolError):
    code = "tool_already_registered"


class ToolValidationError(ToolError):
    code = "tool_validation_error"


class ToolExecutionError(ToolError):
    code = "tool_execution_error"


class ToolTimeoutError(ToolExecutionError):
    code = "tool_timeout"


class MessageProtocolError(TraceFixError):
    code = "message_protocol_error"


class TraceProtocolError(TraceFixError):
    code = "trace_protocol_error"

