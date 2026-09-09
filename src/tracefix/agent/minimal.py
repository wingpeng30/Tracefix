"""TraceFix V0.1 的最小单 Agent 控制循环。"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from tracefix.agent.base import AgentState, AgentStatus, BaseAgent
from tracefix.exceptions import (
    AgentCompleted,
    AgentError,
    AgentLimitExceeded,
    LLMResponseFormatError,
    StepLimitExceeded,
    TestLimitExceeded,
    TimeLimitExceeded,
    TokenBudgetExceeded,
    ToolError,
    ToolExecutionError,
    TraceFixError,
    TraceProtocolError,
    sanitize_payload,
)
from tracefix.messages import Message, MessageRole, ToolCall
from tracefix.models.base import LLMResponse, TokenUsage
from tracefix.tools.base import ReservedToolName, ToolResult
from tracefix.tracing.base import TraceEvent, TraceEventType


class MinimalAgent(BaseAgent):
    """使用原生工具调用完成“分析—执行—反馈”闭环的最小 Agent。"""

    def run(self, task: str) -> AgentState:
        """初始化消息历史并运行，正常完成或预算终止时返回最终状态。"""
        if not task.strip():
            raise AgentError("task cannot be empty")

        self.reset()
        self._task_id = uuid4().hex
        self._started_monotonic = time.monotonic()
        self.state.task = task
        self.state.status = AgentStatus.RUNNING
        self.state.started_at = datetime.now(UTC)

        try:
            return self._run_initialized(task)
        except TraceProtocolError:
            # sink 已经不可用时不能再尝试写事件，只更新内存中的最终状态。
            self.state.status = AgentStatus.FAILED
            self.state.stop_reason = TraceProtocolError.code
            self.state.finished_at = datetime.now(UTC)
            raise

    def _run_initialized(self, task: str) -> AgentState:
        """运行已经完成状态初始化的任务，并集中处理控制流异常。"""
        self._emit(TraceEventType.TASK_STARTED, {"task": task})
        self._emit_state()
        self._append_message(Message(role=MessageRole.SYSTEM, content=self.config.system_prompt))
        self._append_message(Message(role=MessageRole.USER, content=task))

        try:
            while self.state.status is AgentStatus.RUNNING:
                self.step()
        except AgentCompleted as exc:
            self._finish(AgentStatus.COMPLETED, exc.code)
        except AgentLimitExceeded as exc:
            self._emit_error(exc)
            self._finish(AgentStatus.INTERRUPTED, exc.code)
        except KeyboardInterrupt:
            self._finish(AgentStatus.INTERRUPTED, "keyboard_interrupt")
            raise
        except TraceFixError as exc:
            self._emit_error(exc)
            self._finish(AgentStatus.FAILED, exc.code)
            raise
        except Exception as exc:
            wrapped = AgentError(
                f"unexpected agent error: {exc}",
                context={"error_type": type(exc).__name__},
            )
            self._emit_error(wrapped)
            self._finish(AgentStatus.FAILED, wrapped.code)
            raise wrapped from exc

        return self.state

    def step(self) -> None:
        """请求一次模型响应，并顺序执行响应中的全部工具调用。"""
        if self.state.status is not AgentStatus.RUNNING:
            raise AgentError(
                "agent is not running",
                context={"status": self.state.status.value},
            )

        self._check_pre_request_budgets()
        self.state.step_count += 1
        self._emit(
            TraceEventType.MODEL_REQUESTED,
            {"message_count": len(self.history), "tool_names": list(self.tools.names)},
        )

        try:
            response = self.llm.complete(self.history.snapshot(), self.tools.specs())
        except LLMResponseFormatError as exc:
            # 响应解析失败也可能已经产生费用；尽量从异常上下文追回 usage。
            usage = exc.context.get("usage")
            if isinstance(usage, dict):
                self._add_usage_dict(usage)
            raise

        self._add_usage(response.usage)
        self._append_message(response.message)
        self._emit_model_response(response)

        # usage 只能在供应商返回后得知，因此一次调用可能略微越过预算。
        budget_error: AgentLimitExceeded | None = self._post_response_budget_error()
        if budget_error is None:
            try:
                self._check_time_budget()
            except TimeLimitExceeded as exc:
                budget_error = exc
        if budget_error is not None:
            self._append_skipped_results(response.message.tool_calls, budget_error)
            raise budget_error

        if not response.message.tool_calls:
            self.state.final_output = response.message.content or ""
            raise AgentCompleted(
                "model returned a final response",
                context={"final_output": self.state.final_output},
            )

        for index, call in enumerate(response.message.tool_calls):
            try:
                self._check_time_budget()
                if call.name == ReservedToolName.RUN_TESTS.value:
                    if self.state.test_runs >= self.config.max_test_runs:
                        raise TestLimitExceeded(
                            "test run budget exceeded",
                            context={
                                "actual": self.state.test_runs,
                                "limit": self.config.max_test_runs,
                            },
                        )
                    self.state.test_runs += 1
                result = self._execute_tool(call)
            except AgentLimitExceeded as exc:
                # 消息历史要求每个 tool call 都有结果，因此为未执行调用补齐失败消息。
                self._append_skipped_results(response.message.tool_calls[index:], exc)
                raise

            self._append_tool_result(result)

        self._check_time_budget()

    def _execute_tool(self, call: ToolCall) -> ToolResult:
        """执行一个工具；可恢复错误会被转换成结构化失败结果。"""
        self._emit(TraceEventType.TOOL_CALLED, {"call": call.model_dump(mode="json")})
        started = time.monotonic()
        try:
            tool = self.tools.get(call.name)
            result = tool.execute(call)
            if result.call_id != call.id or result.tool_name != call.name:
                raise ToolExecutionError(
                    "tool returned mismatched call metadata",
                    context={
                        "expected_call_id": call.id,
                        "actual_call_id": result.call_id,
                        "expected_tool_name": call.name,
                        "actual_tool_name": result.tool_name,
                    },
                )
            return result
        except ToolError as exc:
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error=exc.message,
                metadata={"error": exc.to_dict()},
                duration_ms=(time.monotonic() - started) * 1000,
            )
        except Exception as exc:
            wrapped = ToolExecutionError(
                f"unexpected tool error: {exc}",
                context={"tool_name": call.name, "error_type": type(exc).__name__},
            )
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error=wrapped.message,
                metadata={"error": wrapped.to_dict()},
                duration_ms=(time.monotonic() - started) * 1000,
            )

    def _append_tool_result(self, result: ToolResult) -> None:
        """把工具结果转换成模型可消费的 tool 消息并写入轨迹。"""
        self._append_message(
            Message(
                role=MessageRole.TOOL,
                content=result.model_dump_json(),
                tool_call_id=result.call_id,
                metadata={"tool_name": result.tool_name, "success": result.success},
            )
        )
        self._emit(
            TraceEventType.TOOL_RETURNED,
            {"result": result.model_dump(mode="json")},
        )

    def _append_skipped_results(
        self,
        calls: tuple[ToolCall, ...],
        reason: AgentLimitExceeded,
    ) -> None:
        """为预算终止后无法执行的调用补写失败结果，保持消息历史闭合。"""
        for call in calls:
            self._append_tool_result(
                ToolResult(
                    call_id=call.id,
                    tool_name=call.name,
                    success=False,
                    error=f"not executed: {reason.message}",
                    metadata={"skipped": True, "reason": reason.code},
                )
            )

    def _check_pre_request_budgets(self) -> None:
        """在产生下一次模型费用前检查所有已知预算。"""
        self._check_time_budget()
        if self.state.step_count >= self.config.max_steps:
            raise StepLimitExceeded(
                "step budget exceeded",
                context={"actual": self.state.step_count, "limit": self.config.max_steps},
            )
        if self.state.input_tokens >= self.config.max_input_tokens:
            raise TokenBudgetExceeded(
                "input token budget exceeded",
                context={
                    "kind": "input",
                    "actual": self.state.input_tokens,
                    "limit": self.config.max_input_tokens,
                },
            )
        if self.state.output_tokens >= self.config.max_output_tokens:
            raise TokenBudgetExceeded(
                "output token budget exceeded",
                context={
                    "kind": "output",
                    "actual": self.state.output_tokens,
                    "limit": self.config.max_output_tokens,
                },
            )

    def _post_response_budget_error(self) -> TokenBudgetExceeded | None:
        """返回模型响应后出现的 Token 越限错误。"""
        if self.state.input_tokens > self.config.max_input_tokens:
            return TokenBudgetExceeded(
                "input token budget exceeded",
                context={
                    "kind": "input",
                    "actual": self.state.input_tokens,
                    "limit": self.config.max_input_tokens,
                },
            )
        if self.state.output_tokens > self.config.max_output_tokens:
            return TokenBudgetExceeded(
                "output token budget exceeded",
                context={
                    "kind": "output",
                    "actual": self.state.output_tokens,
                    "limit": self.config.max_output_tokens,
                },
            )
        return None

    def _check_time_budget(self) -> None:
        """使用单调时钟检查运行时长，避免系统时间调整影响预算。"""
        started = getattr(self, "_started_monotonic", None)
        if started is None:
            return
        elapsed = time.monotonic() - started
        if elapsed >= self.config.wall_time_seconds:
            raise TimeLimitExceeded(
                "wall time budget exceeded",
                context={"actual_seconds": elapsed, "limit": self.config.wall_time_seconds},
            )

    def _add_usage(self, usage: TokenUsage) -> None:
        """累计一次已成功解析的模型调用用量。"""
        self.state.input_tokens += usage.input_tokens
        self.state.output_tokens += usage.output_tokens
        if usage.cost_usd is None:
            # 未知费用不能冒充为零；保留已知部分并标记汇总不完整。
            self.state.cost_complete = False
        else:
            self.state.cost_usd += usage.cost_usd

    def _add_usage_dict(self, usage: dict[str, Any]) -> None:
        """从格式错误上下文中尽力恢复可计费的 usage。"""
        for key, state_field in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
        ):
            value = usage.get(key)
            if isinstance(value, int) and value >= 0:
                setattr(self.state, state_field, getattr(self.state, state_field) + value)
        cost = usage.get("cost_usd")
        if isinstance(cost, (int, float)) and cost >= 0:
            self.state.cost_usd += float(cost)
        else:
            self.state.cost_complete = False

    def _append_message(self, message: Message) -> None:
        """追加消息并同步发送追踪事件。"""
        self.history.append(message)
        self._emit(TraceEventType.MESSAGE_ADDED, {"message": message.model_dump(mode="json")})

    def _emit_model_response(self, response: LLMResponse) -> None:
        """记录规范化后的模型响应与用量，不重复写入原始供应商响应。"""
        self._emit(
            TraceEventType.MODEL_RESPONDED,
            {
                "model_name": response.model_name,
                "finish_reason": response.finish_reason,
                "usage": response.usage.model_dump(mode="json"),
                "message_id": response.message.id,
            },
        )

    def _finish(self, status: AgentStatus, reason: str) -> None:
        """以统一顺序写入最终状态和任务结束事件。"""
        self.state.status = status
        self.state.stop_reason = reason
        self.state.finished_at = datetime.now(UTC)
        self._emit_state()
        self._emit(TraceEventType.TASK_FINISHED, {"state": self.state.model_dump(mode="json")})

    def _emit_state(self) -> None:
        """记录当前完整 Agent 状态。"""
        self._emit(
            TraceEventType.AGENT_STATE_CHANGED,
            {"state": self.state.model_dump(mode="json")},
        )

    def _emit_error(self, error: TraceFixError) -> None:
        """将稳定异常结构写入追踪器。"""
        self._emit(TraceEventType.ERROR, {"error": error.to_dict()})

    def _emit(self, event_type: TraceEventType, payload: dict[str, Any]) -> None:
        """向可选 sink 写入事件，并把 sink 故障映射为轨迹协议异常。"""
        if self.trace_sink is None:
            return
        try:
            self.trace_sink.write(
                TraceEvent(
                    event_type=event_type,
                    task_id=getattr(self, "_task_id", None),
                    step=self.state.step_count,
                    payload=sanitize_payload(payload),
                )
            )
        except TraceProtocolError:
            raise
        except Exception as exc:
            raise TraceProtocolError(
                f"trace sink failed: {exc}",
                context={"event_type": event_type.value, "error_type": type(exc).__name__},
            ) from exc
