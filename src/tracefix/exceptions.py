"""TraceFix 各组件共享的稳定异常体系。"""

from __future__ import annotations

import os
import re
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
_SENSITIVE_KEY_SUFFIXES = (
    "_access_token",
    "_api_key",
    "_credential",
    "_password",
    "_secret",
)
_SECRET_VALUE_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def _is_sensitive_key(value: str) -> bool:
    """识别供应商前缀形式的密钥字段，例如 DEEPSEEK_API_KEY。"""
    normalized = value.casefold()
    return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


def sanitize_payload(value: Any) -> Any:
    """生成适合 JSON 的数据，并脱敏常见凭据字段。"""
    if isinstance(value, Mapping):
        return {
            str(key): (
                "<redacted>"
                if _is_sensitive_key(str(key))
                else sanitize_payload(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, str):
        # 部分供应商异常可能把凭据混入错误文本，额外清理常见 sk- 前缀。
        sanitized = _SECRET_VALUE_PATTERN.sub("<redacted>", value)
        # 请求视图可能包含模型输入的任意文本。除常见前缀外，还要清理当前
        # 进程中名称明确属于凭据的环境变量值，避免非 sk- 形式的 Key 落盘。
        for key, secret in os.environ.items():
            if _is_sensitive_key(key) and len(secret) >= 8:
                sanitized = sanitized.replace(secret, "<redacted>")
        return sanitized
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return repr(value)


class TraceFixError(Exception):
    """携带稳定错误码和结构化上下文的根异常。"""

    code = "tracefix_error"

    def __init__(self, message: str = "", *, context: Mapping[str, Any] | None = None) -> None:
        # 异常文本也可能来自供应商；在对象创建时脱敏，避免直接打印异常时泄漏密钥。
        sanitized_message = sanitize_payload(message or self.__class__.__name__)
        self.message = str(sanitized_message)
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


class ContextError(TraceFixError):
    """上下文估算、裁剪或折叠过程不符合约定。"""

    code = "context_error"


class ContextBudgetExceeded(ContextError):
    """最小安全请求仍超过配置的模型硬窗口。"""

    code = "context_budget_exceeded"


class TraceProtocolError(TraceFixError):
    """轨迹事件或接收器不符合协议。"""

    code = "trace_protocol_error"


class RunConfigurationError(TraceFixError):
    """运行入口的环境变量、密钥或参数组合不完整。"""

    code = "run_configuration_error"


class WorkspaceError(TraceFixError):
    """隔离工作区无法校验、创建或读取。"""

    code = "workspace_error"


class BenchmarkError(TraceFixError):
    """基准任务定义或批量评测过程不符合约定。"""

    code = "benchmark_error"
