"""TraceFix 各组件共享的稳定异常体系。"""

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
    """生成适合 JSON 的数据，并脱敏常见凭据字段。"""
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
    """携带稳定错误码和结构化上下文的根异常。"""

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
    """Agent 生命周期或控制流错误。"""

    code = "agent_error"


class AgentInterrupted(AgentError):
    """用于正常完成或预算终止的控制流中断，不一定代表故障。"""

    code = "agent_interrupted"


class AgentCompleted(AgentInterrupted):
    """模型已经给出最终响应。"""

    code = "agent_completed"


class AgentLimitExceeded(AgentInterrupted):
    """某项 Agent 运行预算已经耗尽。"""

    code = "agent_limit_exceeded"


class StepLimitExceeded(AgentLimitExceeded):
    """模型请求步骤达到上限。"""

    code = "step_limit_exceeded"


class TokenBudgetExceeded(AgentLimitExceeded):
    """输入或输出 Token 达到上限。"""

    code = "token_budget_exceeded"


class TimeLimitExceeded(AgentLimitExceeded):
    """任务运行时间达到上限。"""

    code = "time_limit_exceeded"


class TestLimitExceeded(AgentLimitExceeded):
    """测试调用次数达到上限。"""

    code = "test_limit_exceeded"


class LLMError(TraceFixError):
    """模型请求或响应处理错误。"""

    code = "llm_error"


class LLMAuthenticationError(LLMError):
    """模型供应商认证失败。"""

    code = "llm_authentication_error"


class LLMRateLimitError(LLMError):
    """模型供应商触发限流。"""

    code = "llm_rate_limit_error"


class LLMTimeoutError(LLMError):
    """模型请求超时。"""

    code = "llm_timeout_error"


class LLMContextWindowError(LLMError):
    """输入超过模型上下文窗口。"""

    code = "llm_context_window_error"


class LLMResponseFormatError(LLMError):
    """供应商响应不能被规范化。"""

    code = "llm_response_format_error"


class LLMProviderError(LLMError):
    """其他模型供应商错误。"""

    code = "llm_provider_error"


class ToolError(TraceFixError):
    """工具注册、校验或执行错误。"""

    code = "tool_error"


class ToolNotFoundError(ToolError):
    """模型请求的工具尚未注册。"""

    code = "tool_not_found"


class ToolAlreadyRegisteredError(ToolError):
    """同名工具被重复注册。"""

    code = "tool_already_registered"


class ToolValidationError(ToolError):
    """工具名称、参数或路径校验失败。"""

    code = "tool_validation_error"


class ToolExecutionError(ToolError):
    """工具无法正常启动或完成执行。"""

    code = "tool_execution_error"


class ToolTimeoutError(ToolExecutionError):
    """工具执行超过允许时间。"""

    code = "tool_timeout"


class MessageProtocolError(TraceFixError):
    """消息顺序或工具调用关联不符合协议。"""

    code = "message_protocol_error"


class TraceProtocolError(TraceFixError):
    """轨迹事件或接收器不符合协议。"""

    code = "trace_protocol_error"
