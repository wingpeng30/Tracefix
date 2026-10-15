import hashlib
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
    ContextSessionState,
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
from tracefix.task_state import TaskReference, TaskStateRenderer, TaskStateSession


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
    assert config.compaction_trigger_tokens is None
    assert config.trigger_tokens() == 788_723
    assert config.target_tokens() == 591_542
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


@pytest.mark.parametrize("name", ["get_task_state", "update_task_state"])
def test_task_state_revision_and_item_ids_survive_repeated_tool_pruning(name) -> None:
    call = ToolCall(id="task-state-control", name=name)
    result = ToolResult(call_id=call.id, tool_name=name, success=True, output={
        "revision": 11, "created_ids": ["plan-1"], "evidence_role": "working_state_only",
        "state": {"requirements": ["retained user text" * 1000]},
    })
    history = _history((
        Message(role=MessageRole.ASSISTANT, tool_calls=(call,)),
        Message(role=MessageRole.TOOL, tool_call_id=call.id, content=result.model_dump_json()),
    ))
    manager = ContextManager(ContextConfig(
        compaction_trigger_tokens=9000, context_window_tokens=10000,
        tool_result_threshold_chars=512, tool_result_head_chars=180, tool_result_tail_chars=80,
    ))
    for _ in range(2):
        view = manager.prepare(history)
        output = json.loads(view.messages[-1].content)["output"]
        assert output["revision"] == 11
        assert output["created_ids"] == ["plan-1"]
        assert output["evidence_role"] == "working_state_only"
        history = view.messages


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


def test_artifact_locators_survive_two_context_summary_generations() -> None:
    artifact_id = "a" * 32
    call = ToolCall(id="artifact-call", name="run_tests", arguments={})
    assistant = Message(role=MessageRole.ASSISTANT, tool_calls=(call,))
    result = ToolResult(
        call_id=call.id,
        tool_name=call.name,
        success=True,
        output={"artifact_refs": [{"artifact_id": artifact_id, "tool_name": "run_tests"}]},
    )
    tool = Message(role=MessageRole.TOOL, content=result.model_dump_json(), tool_call_id=call.id)
    manager = ContextManager(ContextConfig())
    first = manager._summarize_batches(((assistant, tool),))
    second = manager._summarize_batches(((first,),))
    assert first.content is not None and second.content is not None
    assert first.content.count(artifact_id) >= 2
    assert second.content.count(artifact_id) >= 2
    assert '"stream":"stdout"' in second.content
    assert '"stream":"stderr"' in second.content


def test_task_state_anchor_survives_two_threshold_compactions() -> None:
    task = "Keep the parser behavior and preserve the original artifact reference."
    proof = {
        "issuer": "tracefix-host-v1",
        "message_id": "user-task-state",
        "turn": 1,
        "content_sha256": hashlib.sha256(task.encode("utf-8")).hexdigest(),
    }
    session = TaskStateSession()
    session.add_user_input(
        message_id="user-task-state",
        turn=1,
        content=task,
        receipt=proof,
        receipt_verified=True,
    )
    state, _ = session.update(
        session.snapshot().revision,
        [{"action": "add", "collection": "plan", "text": "Inspect the parser."}],
    )
    session.add_reference(
        TaskReference(
            kind="artifact",
            reference_id="a" * 32,
            sha256="b" * 64,
            source_message_id="user-task-state",
        )
    )
    state = session.snapshot()

    def anchor() -> Message:
        return Message(
            role=MessageRole.USER,
            content=TaskStateRenderer.render(session.snapshot()),
            metadata={"kind": "task_fact_anchor"},
        )

    first_history = (
        *_history(*(_tool_pair(i, "old evidence " * 100) for i in range(4))),
        anchor(),
    )
    manager = ContextManager(
        ContextConfig(
            context_window_tokens=20_000,
            compaction_trigger_tokens=500,
            retain_ratio=0.001,
            output_reserve_tokens=1,
            safety_margin_ratio=0,
        )
    )
    projection = ContextSessionState()
    first = manager.prepare(first_history, state=projection)
    assert first.compacted and projection.epoch == 1
    assert any(message.metadata.get("kind") == "task_fact_anchor" for message in first.messages)

    state, _ = session.update(
        state.revision,
        [{"action": "add", "collection": "blockers", "text": "Check the regression fixture."}],
    )
    appended_pairs = tuple(_tool_pair(i, "new evidence " * 100) for i in range(5, 9))
    second_history = (
        *first_history,
        *(message for pair in appended_pairs for message in pair),
        anchor(),
    )
    second = manager.prepare(second_history, state=projection)
    assert second.compacted and projection.epoch == 2
    final_anchor = next(
        message for message in second.messages if message.metadata.get("kind") == "task_fact_anchor"
    )
    rendered_state = json.loads(final_anchor.content or "{}")
    assert [item["text"] for item in rendered_state["requirements"]] == [task]
    assert [item["text"] for item in rendered_state["plan"]] == ["Inspect the parser."]
    assert [item["reference_id"] for item in rendered_state["references"]] == ["a" * 32]
    assert [item["text"] for item in rendered_state["blockers"]] == [
        "Check the regression fixture."
    ]


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
    test_call = ToolCall(id="failed-test", name="run_tests", arguments={"command": "pytest -q"})
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


def test_early_failed_test_stays_verbatim_without_pin_all_later_batches() -> None:
    test_call = ToolCall(id="early-failure", name="run_tests", arguments={"command": "pytest -q"})
    messages = (
        Message(role=MessageRole.SYSTEM, content="system"),
        Message(role=MessageRole.USER, content="fix"),
        Message(role=MessageRole.ASSISTANT, tool_calls=(test_call,)),
        Message(
            role=MessageRole.TOOL,
            tool_call_id=test_call.id,
            content=ToolResult(
                call_id=test_call.id,
                tool_name="run_tests",
                success=False,
                error="assertion failed",
                output={"stderr": "early failure"},
            ).model_dump_json(),
        ),
        *_tool_pair(1, "old evidence " * 200),
        *_tool_pair(2, "latest evidence " * 200),
    )
    view = ContextManager(
        ContextConfig(
            compaction_trigger_tokens=300,
            context_window_tokens=10_000,
            retain_ratio=0.1,
            tool_result_threshold_chars=10_000,
        )
    ).prepare(messages)
    assert view.compacted
    assert view.batches_compacted >= 1
    assert any(message.tool_call_id == "early-failure" for message in view.messages)
    assert "early failure" in "\n".join(message.content or "" for message in view.messages)
    assert any(message.metadata.get("tracefix_context_summary") for message in view.messages)


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
        message.metadata.get("tracefix_context_summary") for message in agent.history.snapshot()
    )
    assert any(message.metadata.get("tracefix_context_summary") for message in llm.requests[-1])
    assert state.context_metrics.compaction_count >= 1
    event_types = [event.event_type for event in sink.events]
    assert TraceEventType.CONTEXT_PREPARED in event_types
    assert TraceEventType.CONTEXT_COMPACTED in event_types


def test_optional_request_view_records_actual_compacted_messages_and_redacts_secret(
    monkeypatch,
) -> None:
    """请求视图应等于模型输入，同时不泄漏环境中的非 sk- 形式密钥。"""

    class Sink:
        def __init__(self):
            self.events = []

        def write(self, event):
            self.events.append(event)

        def close(self):
            pass

    secret = "deepseek-secret-without-standard-prefix"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    llm = _ScriptedLLM(
        [_response(call_id="one"), _response(call_id="two"), _response(content="完成")]
    )
    sink = Sink()
    agent = MinimalAgent(
        llm,
        ToolRegistry([_VerboseTool()]),
        AgentConfig(
            record_request_views=True,
            context=ContextConfig(
                compaction_trigger_tokens=300,
                context_window_tokens=10_000,
                retain_ratio=0.2,
                tool_result_threshold_chars=4_000,
                tool_result_head_chars=1_000,
                tool_result_tail_chars=300,
            ),
        ),
        sink,
    )
    agent.run(f"修复问题；临时凭据为 {secret}")

    views = [
        event for event in sink.events if event.event_type is TraceEventType.MODEL_REQUEST_VIEW
    ]
    assert len(views) == len(llm.requests) == 3
    assert views[-1].payload["context"]["compacted"] is True
    assert any(
        message.get("metadata", {}).get("tracefix_context_summary")
        for message in views[-1].payload["messages"]
    )
    serialized = json.dumps([event.model_dump(mode="json") for event in views])
    assert secret not in serialized
    assert "<redacted>" in serialized
    # 完整内存历史不因审计视图而发生裁剪或替换。
    assert secret in (agent.history.snapshot()[1].content or "")


def test_request_view_is_disabled_by_default() -> None:
    class Sink:
        def __init__(self):
            self.events = []

        def write(self, event):
            self.events.append(event)

        def close(self):
            pass

    sink = Sink()
    MinimalAgent(_ScriptedLLM([_response(content="完成")]), trace_sink=sink).run("修复")
    assert not any(event.event_type is TraceEventType.MODEL_REQUEST_VIEW for event in sink.events)


def test_ratio_threshold_uses_output_reserve_and_triggers_at_eighty_percent() -> None:
    messages = _history()
    state = ContextSessionState()
    config = ContextConfig(context_window_tokens=1_000, output_reserve_tokens=100)
    manager = ContextManager(config)
    trigger = config.trigger_tokens(output_reserve_tokens=100)
    below = manager.prepare(
        messages,
        state=state,
        output_reserve_tokens=100,
        token_counter=lambda _messages, _tools: trigger - 1,
    )
    assert below.compacted is False
    at_boundary = ContextManager(config).prepare(
        messages,
        state=state,
        output_reserve_tokens=100,
        token_counter=lambda _messages, _tools: trigger,
    )
    assert at_boundary.compaction_no_op is True
    assert at_boundary.compaction_target_tokens == config.target_tokens(100)
    assert at_boundary.count_method == "official-tokenizer-estimate"
    changed_reserve = ContextManager(config).prepare(
        messages,
        state=ContextSessionState(),
        output_reserve_tokens=200,
        token_counter=lambda _messages, _tools: config.trigger_tokens(200),
    )
    assert changed_reserve.compaction_no_op is True
    assert config.output_reserve_for_request(200) == 200
    assert config.available_input_tokens(200) == 790


def test_minimum_safe_user_content_is_rejected_on_every_compaction_fallback() -> None:
    messages = (
        Message(role=MessageRole.SYSTEM, content="system"),
        Message(role=MessageRole.USER, content="must remain " * 100),
    )
    manager = ContextManager(
        ContextConfig(
            context_window_tokens=500,
            output_reserve_tokens=50,
            safety_margin_ratio=0.01,
        )
    )
    capacity = manager.config.available_input_tokens(50)
    with pytest.raises(ContextBudgetExceeded, match="minimum safe request"):
        manager.prepare(
            messages,
            output_reserve_tokens=50,
            token_counter=lambda _messages, _tools: capacity + 1,
        )


def test_projection_reuses_prefix_and_appends_dynamic_system_messages_in_order() -> None:
    history = _history(_tool_pair(1, "first"))
    state = ContextSessionState()
    manager = ContextManager(ContextConfig(compaction_trigger_tokens=100_000))
    schemas = (ToolSpec(name="read_file", description="read"),)
    first = manager.prepare(history, schemas, state=state)
    skill = Message(
        role=MessageRole.USER,
        content="activated skill",
        metadata={
            "kind": "skill_instructions",
            "trust_level": "untrusted_instruction_data",
        },
    )
    second = manager.prepare((*history, skill), schemas, state=state)
    assert second.projection_reused is True
    assert second.messages[: len(first.messages)] == first.messages
    assert second.messages[-1] == skill
    assert second.messages[0].content == "system"


def test_context_state_rejects_changed_history_and_tool_schema() -> None:
    history = _history(_tool_pair(1))
    schemas = (ToolSpec(name="read_file", description="read"),)
    manager = ContextManager(ContextConfig(compaction_trigger_tokens=100_000))
    state = ContextSessionState()
    manager.prepare(history, schemas, state=state)
    changed_history = (*history[:-1], history[-1].model_copy(update={"content": "changed"}))
    with pytest.raises(ContextBudgetExceeded, match="history changed"):
        manager.prepare(changed_history, schemas, state=state)
    with pytest.raises(ContextBudgetExceeded, match="tool schemas changed"):
        manager.prepare(history, (ToolSpec(name="read_file", description="changed"),), state=state)


def test_three_user_corrections_and_dynamic_skill_survive_compaction_in_order() -> None:
    first = "初始要求：修复解析器。"
    corrections = (
        "第一轮纠正：兼容空输入。",
        "中段关键要求：保留原有错误类型。",
        "第三轮纠正：不要改变公开 API。",
    )
    messages = [Message(role=MessageRole.SYSTEM, content="stable system")]
    messages.append(Message(role=MessageRole.USER, content=first))
    for number, correction in enumerate(corrections, start=1):
        messages.extend(_tool_pair(number, f"evidence {number} " * 120))
        if number == 2:
            messages.append(
                Message(
                    role=MessageRole.USER,
                    content="skill instructions",
                    metadata={
                        "kind": "skill_instructions",
                        "version": "1",
                        "trust_level": "untrusted_instruction_data",
                    },
                )
            )
        messages.append(
            Message(role=MessageRole.USER, content=correction, metadata={"turn": number + 1})
        )
    manager = ContextManager(
        ContextConfig(
            compaction_trigger_tokens=300,
            context_window_tokens=100_000,
            retain_ratio=0.1,
            tool_result_threshold_chars=10_000,
        )
    )
    view = manager.prepare(tuple(messages))
    visible_users = [
        m.content
        for m in view.messages
        if m.role is MessageRole.USER
        and m.metadata.get("kind") not in {"context_summary", "skill_instructions"}
    ]
    assert visible_users == [first, *corrections]
    assert [m.role for m in view.messages if m.content == "skill instructions"] == [
        MessageRole.USER,
    ]
    ordered = (first, corrections[0], "skill instructions", corrections[1], corrections[2])
    positions = [
        next(i for i, message in enumerate(view.messages) if message.content == target)
        for target in ordered
    ]
    assert positions == sorted(positions)


def test_compaction_commits_once_then_reuses_until_the_next_threshold() -> None:
    history = _history(
        *(_tool_pair(number, f"result {number} " + "x" * 1_500) for number in range(1, 21))
    )
    state = ContextSessionState()
    config = ContextConfig(
        context_window_tokens=100_000,
        compaction_trigger_tokens=20_000,
        retain_ratio=0.2,
        tool_result_threshold_chars=10_000,
    )
    manager = ContextManager(config)
    first = manager.prepare(history, state=state, force=True)
    assert first.compacted is True
    assert state.epoch == 1
    appended = (*history, Message(role=MessageRole.ASSISTANT, content="继续处理"))
    reused = manager.prepare(appended, state=state)
    assert reused.projection_reused is True
    assert reused.compacted is False
    assert reused.messages[: len(first.messages)] == first.messages
    pressured = (*appended, Message(role=MessageRole.ASSISTANT, content="z" * 100_000))
    second_epoch = manager.prepare(pressured, state=state)
    assert second_epoch.compacted is True
    assert state.epoch == 2


def test_tokenizer_counter_method_and_partial_tool_visibility_are_explicit() -> None:
    messages = _history(_tool_pair(1, "content"))

    class Estimate:
        tokens = 7
        method = "deepseek-recipe-v41"

    view = ContextManager(ContextConfig(compaction_trigger_tokens=100_000)).prepare(
        messages,
        token_counter=lambda _messages, _tools: Estimate(),
    )
    assert view.estimated_tokens_after == 7
    assert view.count_method == "deepseek-recipe-v41"
    pruned = ContextManager(
        ContextConfig(
            compaction_trigger_tokens=100_000,
            tool_result_threshold_chars=512,
            tool_result_head_chars=180,
            tool_result_tail_chars=80,
        )
    ).prepare(_history(_tool_pair(2, "x" * 800)))
    tool_result = next(message for message in pruned.messages if message.role is MessageRole.TOOL)
    assert tool_result.metadata["context_pruned"] is True
    assert tool_result.tool_call_id in pruned.pruned_tool_call_ids


def test_full_request_counter_cache_is_reused_only_for_the_same_request() -> None:
    history = _history(_tool_pair(1, "content"))
    manager = ContextManager(ContextConfig(compaction_trigger_tokens=100_000))
    state = ContextSessionState()
    calls = []

    def count(messages, tools):
        calls.append(ContextManager._digest_wire(messages, tools))
        return 17

    manager.prepare(history, state=state, token_counter=count)
    manager.prepare(history, state=state, token_counter=count)
    assert len(calls) == 1
    changed = (*history, Message(role=MessageRole.USER, content="new request"))
    manager.prepare(changed, state=state, token_counter=count)
    assert len(calls) == 2


def test_tokenizer_identity_is_pinned_and_restored_with_context_state() -> None:
    history = _history(_tool_pair(1, "content"))

    def count(identity):
        return lambda _messages, _tools: type(
            "Bound",
            (),
            {
                "tokens": 17,
                "status": "estimate",
                "method": "deepseek-recipe-v41",
                "identity": identity,
            },
        )()

    state = ContextSessionState()
    config = ContextConfig(compaction_trigger_tokens=100_000)
    ContextManager(config).prepare(history, state=state, token_counter=count("sha-a"))
    assert state.tokenizer_identity == "sha-a"

    restored = ContextSessionState.model_validate(state.model_dump(mode="json"))
    ContextManager(config).prepare(history, state=restored, token_counter=count("sha-a"))
    with pytest.raises(ContextBudgetExceeded, match="tokenizer identity changed"):
        ContextManager(config).prepare(history, state=restored, token_counter=count("sha-b"))


def test_damaged_pinned_tokenizer_is_blocked_but_unavailable_estimates_fall_back() -> None:
    history = _history(_tool_pair(1, "content"))
    config = ContextConfig(compaction_trigger_tokens=100_000)
    state = ContextSessionState(tokenizer_identity="sha-a")

    class Unavailable:
        tokens = None
        status = "unavailable"
        method = "deepseek-recipe-v41"
        identity = "ValueError"

    with pytest.raises(ContextBudgetExceeded, match="identity could not be verified"):
        ContextManager(config).prepare(
            history,
            state=state,
            token_counter=lambda _messages, _tools: Unavailable(),
        )

    fallback = ContextManager(config).prepare(
        history,
        state=ContextSessionState(),
        token_counter=lambda _messages, _tools: type(
            "Unavailable",
            (),
            {
                "tokens": None,
                "status": "unavailable",
                "method": "deepseek-recipe-v41",
                "identity": "unsupported request settings",
            },
        )(),
    )
    assert fallback.count_method == "tracefix-json-estimate-fallback"
