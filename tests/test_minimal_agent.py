import json
from collections import deque
from dataclasses import dataclass, field

import pytest

from tracefix import (
    AgentConfig,
    AgentError,
    AgentStatus,
    BaseLLM,
    BaseTool,
    LLMConfig,
    LLMProviderError,
    LLMResponse,
    LLMResponseFormatError,
    Message,
    MessageRole,
    MinimalAgent,
    TokenUsage,
    ToolCall,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    TraceEvent,
    TraceEventType,
    TraceProtocolError,
)


class ScriptedLLM(BaseLLM):
    """按顺序返回预设响应，避免单元测试访问真实模型。"""

    def __init__(self, responses: list[LLMResponse | BaseException]) -> None:
        super().__init__(LLMConfig(model_name="scripted"))
        self.responses = deque(responses)
        self.requests = []

    def complete(self, messages, tools=()):
        self.requests.append((messages, tools))
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


@dataclass
class RecordingTool(BaseTool):
    name: str = "search_code"
    calls: list[ToolCall] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="测试工具")

    def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        return ToolResult(
            call_id=call.id,
            tool_name=self.name,
            success=True,
            output={"received": call.arguments},
        )


class MemorySink:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def write(self, event: TraceEvent) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


def response(
    *,
    content: str | None = None,
    calls: tuple[ToolCall, ...] = (),
    input_tokens: int = 10,
    output_tokens: int = 2,
    cost_usd: float = 0.01,
) -> LLMResponse:
    return LLMResponse(
        message=Message(role=MessageRole.ASSISTANT, content=content, tool_calls=calls),
        usage=TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=cost_usd,
        ),
        model_name="scripted",
        finish_reason="tool_calls" if calls else "stop",
    )


def test_agent_runs_tool_loop_and_accumulates_usage() -> None:
    tool = RecordingTool()
    sink = MemorySink()
    llm = ScriptedLLM(
        [
            response(
                calls=(ToolCall(id="call-1", name="search_code", arguments={"query": "bug"}),)
            ),
            response(content="修复完成", input_tokens=20, output_tokens=4, cost_usd=0.02),
        ]
    )
    agent = MinimalAgent(llm, ToolRegistry([tool]), trace_sink=sink)

    state = agent.run("修复这个问题")

    assert state.status is AgentStatus.COMPLETED
    assert state.final_output == "修复完成"
    assert state.stop_reason == "agent_completed"
    assert state.step_count == 2
    assert state.input_tokens == 30
    assert state.output_tokens == 6
    assert state.cost_usd == pytest.approx(0.03)
    assert len(tool.calls) == 1
    assert len(llm.requests[1][0]) == 4

    tool_message = agent.history.snapshot()[3]
    payload = json.loads(tool_message.content or "")
    assert tool_message.role is MessageRole.TOOL
    assert payload["call_id"] == "call-1"
    assert payload["success"] is True
    assert agent.history.pending_tool_call_ids == frozenset()
    assert TraceEventType.TOOL_CALLED in [event.event_type for event in sink.events]
    assert sink.events[-1].event_type is TraceEventType.TASK_FINISHED
    assert len({event.task_id for event in sink.events}) == 1


def test_unknown_tool_becomes_feedback_and_agent_can_recover() -> None:
    llm = ScriptedLLM(
        [
            response(calls=(ToolCall(id="missing-1", name="missing_tool"),)),
            response(content="无法使用该工具，但已停止"),
        ]
    )
    agent = MinimalAgent(llm)

    state = agent.run("尝试未知工具")

    assert state.status is AgentStatus.COMPLETED
    failed_result = json.loads(agent.history.snapshot()[3].content or "")
    assert failed_result["success"] is False
    assert failed_result["metadata"]["error"]["code"] == "tool_not_found"


def test_step_limit_interrupts_after_allowed_request() -> None:
    tool = RecordingTool()
    llm = ScriptedLLM(
        [response(calls=(ToolCall(id="call-1", name="search_code"),))]
    )
    agent = MinimalAgent(
        llm,
        ToolRegistry([tool]),
        AgentConfig(max_steps=1),
    )

    state = agent.run("一步后停止")

    assert state.status is AgentStatus.INTERRUPTED
    assert state.stop_reason == "step_limit_exceeded"
    assert state.step_count == 1
    assert agent.history.pending_tool_call_ids == frozenset()


def test_token_overrun_skips_pending_tools() -> None:
    tool = RecordingTool()
    llm = ScriptedLLM(
        [
            response(
                calls=(ToolCall(id="call-1", name="search_code"),),
                input_tokens=6,
            )
        ]
    )
    agent = MinimalAgent(
        llm,
        ToolRegistry([tool]),
        AgentConfig(max_input_tokens=5),
    )

    state = agent.run("超过 token")

    assert state.status is AgentStatus.INTERRUPTED
    assert state.stop_reason == "token_budget_exceeded"
    assert state.input_tokens == 6
    assert tool.calls == []
    skipped = json.loads(agent.history.snapshot()[-1].content or "")
    assert skipped["metadata"]["skipped"] is True
    assert agent.history.pending_tool_call_ids == frozenset()


def test_test_budget_stops_multi_call_response_without_dangling_calls() -> None:
    test_tool = RecordingTool(name="run_tests")
    calls = (
        ToolCall(id="test-1", name="run_tests"),
        ToolCall(id="test-2", name="run_tests"),
        ToolCall(id="search-1", name="search_code"),
    )
    search_tool = RecordingTool()
    agent = MinimalAgent(
        ScriptedLLM([response(calls=calls)]),
        ToolRegistry([test_tool, search_tool]),
        AgentConfig(max_test_runs=1),
    )

    state = agent.run("测试预算")

    assert state.status is AgentStatus.INTERRUPTED
    assert state.stop_reason == "test_limit_exceeded"
    assert state.test_runs == 1
    assert len(test_tool.calls) == 1
    assert search_tool.calls == []
    assert agent.history.pending_tool_call_ids == frozenset()


def test_wall_time_budget_is_checked_before_model_request(monkeypatch) -> None:
    clock = iter([10.0, 12.0])
    monkeypatch.setattr("tracefix.agent.minimal.time.monotonic", lambda: next(clock))
    agent = MinimalAgent(
        ScriptedLLM([response(content="不应被调用")]),
        config=AgentConfig(wall_time_seconds=1),
    )

    state = agent.run("超时")

    assert state.status is AgentStatus.INTERRUPTED
    assert state.stop_reason == "time_limit_exceeded"
    assert state.step_count == 0


def test_wall_time_overrun_after_model_response_interrupts(monkeypatch) -> None:
    clock = iter([10.0, 10.0, 12.0])
    monkeypatch.setattr("tracefix.agent.minimal.time.monotonic", lambda: next(clock))
    agent = MinimalAgent(
        ScriptedLLM([response(content="返回得太晚")]),
        config=AgentConfig(wall_time_seconds=1),
    )
    state = agent.run("响应后超时")
    assert state.status is AgentStatus.INTERRUPTED
    assert state.stop_reason == "time_limit_exceeded"
    assert state.final_output is None


def test_unrecoverable_llm_error_marks_agent_failed() -> None:
    error = LLMProviderError("provider unavailable")
    agent = MinimalAgent(ScriptedLLM([error]))

    with pytest.raises(LLMProviderError) as captured:
        agent.run("触发供应商错误")

    assert captured.value is error
    assert agent.state.status is AgentStatus.FAILED
    assert agent.state.stop_reason == "llm_provider_error"
    assert agent.state.finished_at is not None


def test_empty_task_and_step_outside_run_are_rejected() -> None:
    agent = MinimalAgent(ScriptedLLM([response(content="unused")]))
    with pytest.raises(AgentError):
        agent.run("   ")
    with pytest.raises(AgentError):
        agent.step()
    agent._check_time_budget()


def test_unexpected_llm_error_is_wrapped() -> None:
    agent = MinimalAgent(ScriptedLLM([ValueError("boom")]))
    with pytest.raises(AgentError) as captured:
        agent.run("触发未知错误")
    assert isinstance(captured.value.__cause__, ValueError)
    assert agent.state.status is AgentStatus.FAILED


def test_keyboard_interrupt_updates_state_then_propagates() -> None:
    agent = MinimalAgent(ScriptedLLM([KeyboardInterrupt()]))
    with pytest.raises(KeyboardInterrupt):
        agent.run("中断任务")
    assert agent.state.status is AgentStatus.INTERRUPTED
    assert agent.state.stop_reason == "keyboard_interrupt"


def test_mismatched_and_crashing_tools_become_failed_results() -> None:
    class MismatchedTool(RecordingTool):
        def execute(self, call: ToolCall) -> ToolResult:
            return ToolResult(
                call_id="wrong-id",
                tool_name=self.name,
                success=True,
                output="wrong",
            )

    class CrashingTool(RecordingTool):
        def execute(self, call: ToolCall) -> ToolResult:
            raise RuntimeError("boom")

    llm = ScriptedLLM(
        [
            response(
                calls=(
                    ToolCall(id="bad-1", name="bad_metadata"),
                    ToolCall(id="bad-2", name="crashing"),
                )
            ),
            response(content="收到失败反馈"),
        ]
    )
    registry = ToolRegistry(
        [MismatchedTool(name="bad_metadata"), CrashingTool(name="crashing")]
    )
    agent = MinimalAgent(llm, registry)

    state = agent.run("工具错误")

    assert state.status is AgentStatus.COMPLETED
    first = json.loads(agent.history.snapshot()[3].content or "")
    second = json.loads(agent.history.snapshot()[4].content or "")
    assert first["metadata"]["error"]["code"] == "tool_execution_error"
    assert second["metadata"]["error"]["context"]["error_type"] == "RuntimeError"


@pytest.mark.parametrize(
    ("config", "first_usage", "expected_kind"),
    [
        (AgentConfig(max_input_tokens=10), {"input_tokens": 10}, "input"),
        (AgentConfig(max_output_tokens=2), {"output_tokens": 2}, "output"),
    ],
)
def test_exact_token_limit_blocks_the_next_model_request(
    config: AgentConfig,
    first_usage: dict[str, int],
    expected_kind: str,
) -> None:
    tool = RecordingTool()
    llm = ScriptedLLM(
        [
            response(
                calls=(ToolCall(id="call-1", name="search_code"),),
                input_tokens=first_usage.get("input_tokens", 1),
                output_tokens=first_usage.get("output_tokens", 1),
            )
        ]
    )
    agent = MinimalAgent(llm, ToolRegistry([tool]), config)
    state = agent.run("精确达到预算")
    assert state.status is AgentStatus.INTERRUPTED
    assert state.stop_reason == "token_budget_exceeded"
    assert len(llm.requests) == 1
    assert expected_kind in {"input", "output"}


def test_output_token_overrun_interrupts_immediately() -> None:
    agent = MinimalAgent(
        ScriptedLLM([response(content="过长", output_tokens=3)]),
        config=AgentConfig(max_output_tokens=2),
    )
    state = agent.run("输出越限")
    assert state.status is AgentStatus.INTERRUPTED
    assert state.final_output is None
    assert state.output_tokens == 3


def test_format_error_usage_is_preserved() -> None:
    error = LLMResponseFormatError(
        "bad response",
        context={
            "usage": {"input_tokens": 7, "output_tokens": 3, "cost_usd": 0.25}
        },
    )
    agent = MinimalAgent(ScriptedLLM([error]))
    with pytest.raises(LLMResponseFormatError):
        agent.run("格式错误")
    assert agent.state.input_tokens == 7
    assert agent.state.output_tokens == 3
    assert agent.state.cost_usd == pytest.approx(0.25)


def test_trace_sink_failure_maps_to_protocol_error() -> None:
    class BrokenSink:
        def write(self, event: TraceEvent) -> None:
            raise OSError("disk unavailable")

        def close(self) -> None:
            pass

    agent = MinimalAgent(ScriptedLLM([response(content="unused")]), trace_sink=BrokenSink())
    with pytest.raises(TraceProtocolError) as captured:
        agent.run("追踪失败")
    assert isinstance(captured.value.__cause__, OSError)
    assert agent.state.status is AgentStatus.FAILED
    assert agent.state.stop_reason == "trace_protocol_error"


def test_existing_trace_protocol_error_is_not_wrapped() -> None:
    error = TraceProtocolError("already normalized")

    class ProtocolSink:
        def write(self, event: TraceEvent) -> None:
            raise error

        def close(self) -> None:
            pass

    agent = MinimalAgent(ScriptedLLM([response(content="unused")]), trace_sink=ProtocolSink())
    with pytest.raises(TraceProtocolError) as captured:
        agent.run("协议错误")
    assert captured.value is error
