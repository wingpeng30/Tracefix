import json
from collections import deque

import pytest

from tracefix import (
    AgentConfig,
    BaseLLM,
    BaseTool,
    ContextBudgetExceeded,
    ContextConfig,
    ContextManager,
    LLMConfig,
    LLMResponse,
    Message,
    MessageRole,
    MinimalAgent,
    TokenUsage,
    ToolCall,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    TraceEventType,
)


def _tool_pair(number: int, output: str = "ok") -> tuple[Message, Message]:
    call = ToolCall(
        id=f"call-{number}",
        name="read_file",
        arguments={"path": f"module_{number}.py", "start_line": 1},
    )
    assistant = Message(role=MessageRole.ASSISTANT, content="读取文件", tool_calls=(call,))
    result = ToolResult(
        call_id=call.id,
        tool_name=call.name,
        success=True,
        output={"path": f"module_{number}.py", "content": output},
    )
    tool = Message(role=MessageRole.TOOL, content=result.model_dump_json(), tool_call_id=call.id)
    return assistant, tool


def _history(*pairs: tuple[Message, Message]) -> tuple[Message, ...]:
    return (
        Message(role=MessageRole.SYSTEM, content="system"),
        Message(role=MessageRole.USER, content="修复问题"),
        *(message for pair in pairs for message in pair),
    )


def test_default_config_and_unchanged_small_view() -> None:
    config = ContextConfig()
    assert config.context_window_tokens == 1_000_000
    assert config.compaction_trigger_tokens == 32_000
    messages = _history(_tool_pair(1))
    view = ContextManager(config).prepare(messages)
    assert view.compacted is False
    assert [message.model_dump() for message in view.messages] == [
        message.model_dump() for message in messages
    ]
    assert view.messages[0] is not messages[0]


def test_oversized_tool_result_is_pruned_as_valid_json() -> None:
    messages = _history(_tool_pair(1, "A" * 700 + "TAIL"))
    manager = ContextManager(
        ContextConfig(
            compaction_trigger_tokens=9_000,
            context_window_tokens=10_000,
            tool_result_threshold_chars=512,
            tool_result_head_chars=180,
            tool_result_tail_chars=80,
        )
    )
    view = manager.prepare(messages)
    payload = json.loads(view.messages[-1].content or "")
    assert view.tool_results_pruned == 1
    assert payload["call_id"] == "call-1"
    assert payload["output"]["_tracefix_pruned"] is True
    assert "TraceFix 已裁剪" in payload["output"]["content"]
    assert "TAIL" in payload["output"]["content"]
    assert "A" * 700 in (messages[-1].content or "")


def test_pressure_compacts_whole_batches_into_deterministic_summary() -> None:
    messages = _history(
        _tool_pair(1, "first evidence" * 60),
        _tool_pair(2, "second evidence" * 60),
        _tool_pair(3, "latest evidence" * 60),
    )
    manager = ContextManager(
        ContextConfig(
            compaction_trigger_tokens=300,
            context_window_tokens=10_000,
            retain_ratio=0.2,
            tool_result_threshold_chars=4_000,
            tool_result_head_chars=1_000,
            tool_result_tail_chars=300,
        )
    )
    view = manager.prepare(messages)
    assert view.compacted is True
    assert view.messages_compacted == view.batches_compacted * 2
    summary = view.messages[2]
    assert summary.metadata["tracefix_context_summary"] is True
    assert "read_file" in (summary.content or "")
    assert "module_1.py" in (summary.content or "")

    for index, message in enumerate(view.messages):
        if message.role is MessageRole.ASSISTANT and message.tool_calls:
            result_ids = {
                item.tool_call_id
                for item in view.messages[index + 1 : index + 1 + len(message.tool_calls)]
            }
            assert result_ids == {call.id for call in message.tool_calls}


def test_latest_failed_test_is_kept_verbatim_and_old_tools_are_summarized() -> None:
    old_calls = tuple(
        ToolCall(id=f"old-{name}", name=name, arguments={"value": name})
        for name in ("search_code", "apply_patch", "get_git_diff")
    )
    old_assistant = Message(role=MessageRole.ASSISTANT, tool_calls=old_calls)
    old_results = tuple(
        Message(
            role=MessageRole.TOOL,
            tool_call_id=call.id,
            content=ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=True,
                output={"evidence": call.name * 80},
            ).model_dump_json(),
        )
        for call in old_calls
    )
    test_call = ToolCall(
        id="failed-test", name="run_tests", arguments={"command": "pytest -q"}
    )
    failed_test = (
        Message(role=MessageRole.ASSISTANT, tool_calls=(test_call,)),
        Message(
            role=MessageRole.TOOL,
            tool_call_id=test_call.id,
            content=ToolResult(
                call_id=test_call.id,
                tool_name=test_call.name,
                success=False,
                error="assertion failed",
                output={"stderr": "AssertionError: expected 2"},
            ).model_dump_json(),
        ),
    )
    messages = (
        Message(role=MessageRole.SYSTEM, content="system"),
        Message(role=MessageRole.USER, content="fix"),
        old_assistant,
        *old_results,
        *failed_test,
        *_tool_pair(9, "latest" * 100),
    )
    view = ContextManager(
        ContextConfig(
            compaction_trigger_tokens=300,
            context_window_tokens=10_000,
            retain_ratio=0.1,
            tool_result_threshold_chars=4_000,
            tool_result_head_chars=1_000,
            tool_result_tail_chars=300,
        )
    ).prepare(messages)
    summary = view.messages[2].content or ""
    assert all(name in summary for name in ("search_code", "apply_patch", "get_git_diff"))
    assert any(message.tool_call_id == "failed-test" for message in view.messages)
    assert "AssertionError: expected 2" in "\n".join(
        message.content or "" for message in view.messages
    )


def test_hard_limit_and_invalid_configuration() -> None:
    messages = _history(_tool_pair(1, "x" * 200))
    manager = ContextManager(
        ContextConfig(
            enabled=False,
            context_window_tokens=20,
            compaction_trigger_tokens=20,
        )
    )
    with pytest.raises(ContextBudgetExceeded):
        manager.prepare(messages)
    with pytest.raises(ValueError):
        ContextConfig(context_window_tokens=100, compaction_trigger_tokens=101)
    with pytest.raises(ValueError):
        ContextConfig(
            tool_result_threshold_chars=256,
            tool_result_head_chars=200,
            tool_result_tail_chars=100,
        )


class _ScriptedLLM(BaseLLM):
    def __init__(self, responses):
        super().__init__(LLMConfig(model_name="scripted"))
        self.responses = deque(responses)
        self.requests = []

    def complete(self, messages, tools=()):
        self.requests.append(messages)
        return self.responses.popleft()


class _VerboseTool(BaseTool):
    @property
    def spec(self):
        return ToolSpec(name="read_file", description="读取")

    def execute(self, call):
        return ToolResult(
            call_id=call.id,
            tool_name=call.name,
            success=True,
            output={"path": call.arguments["path"], "content": "evidence" * 150},
        )


def _response(content=None, call_id=None):
    calls = ()
    if call_id:
        calls = (
            ToolCall(
                id=call_id,
                name="read_file",
                arguments={"path": f"{call_id}.py"},
            ),
        )
    return LLMResponse(
        message=Message(role=MessageRole.ASSISTANT, content=content, tool_calls=calls),
        usage=TokenUsage(input_tokens=10, output_tokens=2, total_tokens=12, cost_usd=0.01),
        model_name="scripted",
    )


def test_agent_uses_compacted_view_while_full_history_and_trace_remain() -> None:
    class Sink:
        def __init__(self):
            self.events = []

        def write(self, event):
            self.events.append(event)

        def close(self):
            pass

    llm = _ScriptedLLM(
        [_response(call_id="one"), _response(call_id="two"), _response(content="完成")]
    )
    sink = Sink()
    agent = MinimalAgent(
        llm,
        ToolRegistry([_VerboseTool()]),
        AgentConfig(
            context=ContextConfig(
                compaction_trigger_tokens=300,
                context_window_tokens=10_000,
                retain_ratio=0.2,
                tool_result_threshold_chars=4_000,
                tool_result_head_chars=1_000,
                tool_result_tail_chars=300,
            )
        ),
        sink,
    )
    state = agent.run("修复")
    assert len(agent.history.snapshot()) == 7
    assert not any(
        message.metadata.get("tracefix_context_summary")
        for message in agent.history.snapshot()
    )
    assert any(
        message.metadata.get("tracefix_context_summary") for message in llm.requests[-1]
    )
    assert state.context_metrics.compaction_count >= 1
    event_types = [event.event_type for event in sink.events]
    assert TraceEventType.CONTEXT_PREPARED in event_types
    assert TraceEventType.CONTEXT_COMPACTED in event_types
