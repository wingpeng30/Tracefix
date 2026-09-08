import json
from datetime import datetime

import pytest
from pydantic import ValidationError

from tracefix import Message, MessageHistory, MessageProtocolError, MessageRole, ToolCall


def test_message_timestamp_is_timezone_aware() -> None:
    message = Message(role=MessageRole.USER, content="hello")
    assert message.timestamp.tzinfo is not None
    assert message.timestamp.utcoffset() is not None


def test_message_rejects_invalid_role_shape() -> None:
    with pytest.raises(ValidationError):
        Message(role=MessageRole.TOOL, content="result")

    with pytest.raises(ValidationError):
        Message(role=MessageRole.USER, content=None)

    with pytest.raises(ValidationError):
        Message(role=MessageRole.USER, content="x", tool_call_id="call-1")

    with pytest.raises(ValidationError):
        Message(role=MessageRole.USER, content="x", timestamp=datetime(2026, 1, 1))


def test_history_correlates_tool_calls_and_round_trips_json() -> None:
    history = MessageHistory()
    history.extend(
        [
            Message(role=MessageRole.USER, content="search"),
            Message(
                role=MessageRole.ASSISTANT,
                tool_calls=(
                    ToolCall(id="call-1", name="search_code", arguments={"query": "Parser"}),
                ),
            ),
            Message(role=MessageRole.TOOL, tool_call_id="call-1", content="src/parser.py"),
        ]
    )

    encoded = history.to_json()
    decoded = MessageHistory.from_json(encoded)

    assert len(decoded) == 3
    assert decoded.pending_tool_call_ids == frozenset()
    assert json.loads(encoded)[1]["tool_calls"][0]["name"] == "search_code"


def test_history_rejects_unknown_and_duplicate_tool_results() -> None:
    history = MessageHistory()
    with pytest.raises(MessageProtocolError):
        history.append(Message(role=MessageRole.TOOL, tool_call_id="missing", content="nope"))

    history.append(
        Message(
            role=MessageRole.ASSISTANT,
            tool_calls=(ToolCall(id="call-1", name="read_file"),),
        )
    )
    history.append(Message(role=MessageRole.TOOL, tool_call_id="call-1", content="ok"))
    with pytest.raises(MessageProtocolError):
        history.append(Message(role=MessageRole.TOOL, tool_call_id="call-1", content="again"))


def test_extend_is_atomic_and_snapshot_is_detached() -> None:
    original = Message(role=MessageRole.USER, content="start", metadata={"nested": {"value": 1}})
    history = MessageHistory([original])

    with pytest.raises(MessageProtocolError):
        history.extend(
            [
                Message(
                    role=MessageRole.ASSISTANT,
                    tool_calls=(ToolCall(id="call-1", name="read_file"),),
                ),
                Message(role=MessageRole.USER, content="too early"),
            ]
        )
    assert len(history) == 1

    snapshot = history.snapshot()
    snapshot[0].metadata["nested"] = {"value": 99}
    assert history.snapshot()[0].metadata["nested"] == {"value": 1}


def test_append_is_atomic_when_a_later_call_id_is_duplicate() -> None:
    history = MessageHistory(
        [
            Message(
                role=MessageRole.ASSISTANT,
                tool_calls=(ToolCall(id="existing", name="read_file"),),
            ),
            Message(role=MessageRole.TOOL, tool_call_id="existing", content="done"),
        ]
    )

    with pytest.raises(MessageProtocolError):
        history.append(
            Message(
                role=MessageRole.ASSISTANT,
                tool_calls=(
                    ToolCall(id="new-call", name="search_code"),
                    ToolCall(id="existing", name="read_file"),
                ),
            )
        )

    assert len(history) == 2
    assert history.pending_tool_call_ids == frozenset()
