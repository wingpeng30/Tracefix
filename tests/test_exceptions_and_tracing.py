from tracefix import (
    LLMAuthenticationError,
    TraceEvent,
    TraceEventType,
    TraceFixError,
    TraceSink,
)


def test_exception_is_structured_and_redacts_secrets() -> None:
    error = LLMAuthenticationError(
        "bad credentials",
        context={"api_key": "secret-value", "nested": {"authorization": "Bearer token"}},
    )
    payload = error.to_dict()
    assert isinstance(error, TraceFixError)
    assert payload["code"] == "llm_authentication_error"
    assert payload["context"]["api_key"] == "<redacted>"
    assert payload["context"]["nested"]["authorization"] == "<redacted>"


def test_trace_event_is_json_serializable() -> None:
    event = TraceEvent(
        event_type=TraceEventType.TOOL_RETURNED,
        task_id="task-1",
        step=2,
        payload={"returncode": 0},
    )
    dumped = event.model_dump(mode="json")
    assert dumped["event_type"] == "tool_returned"
    assert dumped["timestamp"].endswith("Z")


def test_trace_sink_protocol_is_runtime_checkable() -> None:
    class Sink:
        def write(self, event: TraceEvent) -> None:
            pass

        def close(self) -> None:
            pass

    assert isinstance(Sink(), TraceSink)


