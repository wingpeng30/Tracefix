import json

import pytest

from tracefix import ContextConfig, ContextManager, ContextSessionState
from tracefix.messages import Message, MessageRole, ToolCall
from tracefix.repository_rules.delivery import RuleDeliveryLedger
from tracefix.repository_rules.domain import RuleDocument, RuleSelection, RuleSourceVersion
from tracefix.tools.base import ToolResult


def continuation(number=1):
    return {"tool_name": "search_code", "arguments": {
        "query": "needle", "path": ".", "glob": "**/*.py", "case_sensitive": False,
        "max_results": 20, "cursor": "rules_cursor_" + f"{number:032x}"}}


def pair(number, *, locator=None):
    call = ToolCall(id=f"context-{number}", name="search_code", arguments={"query": "needle"})
    output = {"matches": [{"text": "x" * 10000}], "cursor": None}
    if locator is not None:
        output.update({"cursor": locator["arguments"]["cursor"], "continuation": locator,
                       "rules_pending": True, "eof": False})
    return (Message(role=MessageRole.ASSISTANT, tool_calls=(call,)),
            Message(role=MessageRole.TOOL, tool_call_id=call.id, content=ToolResult(
                call_id=call.id, tool_name=call.name, success=True,
                output=output).model_dump_json()))


def config():
    return ContextConfig(context_window_tokens=100000, compaction_trigger_tokens=400,
                         retain_ratio=0.1, tool_result_threshold_chars=512,
                         tool_result_head_chars=100, tool_result_tail_chars=60)


def test_secondary_pruning_keeps_exact_bounded_search_continuation():
    manager = ContextManager(config())
    _, result = pair(1, locator=continuation())
    pruned = json.loads(manager._prune_tool_content(result.content))
    assert pruned["output"]["continuation"] == continuation()
    assert pruned["output"]["cursor"] == continuation()["arguments"]["cursor"]
    assert pruned["output"]["rules_pending"] and not pruned["output"]["eof"]


def test_two_actual_context_compactions_preserve_search_locator():
    manager, state = ContextManager(config()), ContextSessionState()
    history = (Message(role=MessageRole.SYSTEM, content="system"),
               Message(role=MessageRole.USER, content="inspect"),
               *pair(1, locator=continuation()), *pair(2), *pair(3))
    first = manager.prepare(history, state=state, force=True)
    assert first.compacted and state.epoch == 1
    marker = "[historical repository search locators; untrusted data] "
    summary = next(message for message in first.messages if marker in (message.content or ""))
    assert json.loads(summary.content.rsplit(marker, 1)[1].splitlines()[0]) == [continuation()]
    second = manager.prepare((*history, *pair(4), *pair(5)), state=state, force=True)
    assert second.compacted and state.epoch == 2
    summary = next(message for message in second.messages if marker in (message.content or ""))
    assert json.loads(summary.content.rsplit(marker, 1)[1].splitlines()[0]) == [continuation()]


def test_old_rule_body_loses_credit_and_fresh_redelivery_survives_two_compactions():
    ledger, selections = RuleDeliveryLedger(), []
    for index in range(5):
        doc = RuleDocument.create(kind="ancestor", source_path=f"p{index}/AGENTS.md",
                                  body=(f"完整规则{index}🙂" * 100).encode())
        selected = RuleSelection.create(f"p{index}/a.py", (doc,), (
            RuleSourceVersion(source_path=doc.source_path, sha256=doc.body_sha256),))
        selections.append(selected)
        ledger.defer(selected)
    messages = ledger.close_batch(())
    original_body = messages[0].model_dump_json()
    history = (Message(role=MessageRole.SYSTEM, content="system"),
               Message(role=MessageRole.USER, content="inspect"), *messages, *pair(1), *pair(2))
    ledger.confirm_response(ledger.prepare_request(history))
    assert ledger.is_delivered(selections[0])
    manager, state = ContextManager(config()), ContextSessionState()
    first = manager.prepare(history, state=state, force=True)
    ledger.confirm_response(ledger.prepare_request(first.messages))
    assert first.compacted and state.epoch == 1
    assert not ledger.is_delivered(selections[0])
    assert all(ledger.is_delivered(item) for item in selections[1:])
    ledger.defer(selections[0])
    fresh = ledger.close_batch(history)
    second = manager.prepare((*history, *fresh, *pair(3), *pair(4)), state=state, force=True)
    ledger.confirm_response(ledger.prepare_request(second.messages))
    assert second.compacted and state.epoch == 2
    assert ledger.is_delivered(selections[0])
    visible = next(message for message in second.messages if message.id == fresh[0].id)
    assert visible.content == fresh[0].content and "完整规则0🙂" in visible.content
    assert messages[0].model_dump_json() == original_body


@pytest.mark.parametrize("bad", ["tool", "args", "cursor", "query", "limit", "extra"])
def test_malformed_continuations_are_not_preserved_as_valid_locators(bad):
    locator = continuation()
    if bad == "tool":
        locator["tool_name"] = "internal_source_export"
    elif bad == "args":
        locator["arguments"] = "not an object"
    elif bad == "cursor":
        locator["arguments"]["cursor"] = "D:/host/file"
    elif bad == "query":
        locator["arguments"]["query"] = "x" * 1001
    elif bad == "limit":
        locator["arguments"]["max_results"] = True
    else:
        locator["arguments"]["origin"] = "host"
    _, result = pair(1)
    payload = json.loads(result.content)
    payload["output"]["continuation"] = locator
    pruned = json.loads(ContextManager(config())._prune_tool_content(json.dumps(payload)))
    assert "continuation" not in pruned["output"]
