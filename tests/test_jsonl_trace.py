import json

import pytest

from tracefix import JSONLTraceSink, TraceEvent, TraceEventType, TraceProtocolError


def test_jsonl_sink_flushes_valid_events_and_closes_idempotently(tmp_path) -> None:
    path = tmp_path / "trace" / "events.jsonl"
    sink = JSONLTraceSink(path)
    first = TraceEvent(event_type=TraceEventType.TASK_STARTED, payload={"中文": "任务"})
    second = TraceEvent(event_type=TraceEventType.TASK_FINISHED, payload={"ok": True})

    sink.write(first)
    # write() 会立即刷新，因此关闭之前文件内容已经可读。
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["id"] == first.id
    sink.write(second)
    sink.close()
    sink.close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["event_type"] for row in rows] == ["task_started", "task_finished"]
    assert sink.closed


def test_jsonl_sink_context_manager_and_closed_write(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    with JSONLTraceSink(path) as sink:
        sink.write(TraceEvent(event_type=TraceEventType.ERROR, payload={"message": "x"}))

    with pytest.raises(TraceProtocolError):
        sink.write(TraceEvent(event_type=TraceEventType.ERROR))
