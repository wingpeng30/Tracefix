from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tracefix.artifacts import ArtifactStore
from tracefix.jobs.artifact_reader import RemoteJobArtifactReader
from tracefix.jobs.domain import JobError, JobRecord, JobStatus
from tracefix.jobs.remote import _digest
from tracefix.messages import ToolCall
from tracefix.tools.artifacts import ReadToolArtifactTool
from tracefix.tools.base import ToolResult


def fixture(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts", run_id="r1", streaming_enabled=True)
    ref = store.publish_process(call_id="test1", tool_name="run_tests",
        source_identity={"diff": "old"},
        stdout="中段🙂旧日志".encode(), stderr=b"warning", complete=True)
    result = ToolResult(call_id="test1", tool_name="run_tests", success=False,
        error="cancelled", output={"artifact_refs": [ref.model_dump(mode="json")]})
    record = JobRecord(identity={"job_id": "job_" + "a" * 32, "run_id": "r1",
        "call_id": "test1", "execution_id": "b" * 32, "invocation_sha256": "c" * 64},
        status=JobStatus.CANCELLED, process_started=True, tree_exit_confirmed=True,
        created_at=datetime.now(UTC), deadline=datetime.now(UTC) + timedelta(seconds=10),
        outcome_sha256=_digest(result))
    requests = []
    tool = ReadToolArtifactTool(store)

    def call(name, args):
        requests.append(name)
        return tool.execute(ToolCall(id="read1", name=name, arguments=args))

    channel = SimpleNamespace(call=call)
    reader = RemoteJobArtifactReader(channel, run_id="r1", result_provider=lambda job: result)
    return reader, record, result, store, requests


def test_old_job_reads_current_restored_artifact_with_shared_unicode_quota(tmp_path):
    reader, record, _, store, requests = fixture(tmp_path)
    page = reader.read(record, stream="stdout", offset=2, limit=3)
    assert page["content"] == "🙂旧日" and page["final"]
    assert page["job_id"] == record.identity.job_id
    assert store.quota().queries == 1 and store.quota().returned_chars == 3
    assert requests == ["read_tool_artifact"]


@pytest.mark.parametrize("field,value", [("artifact_id", "f" * 32), ("view_sha256", "0" * 64),
    ("source_identity", {}), ("offset", 100), ("next_offset", 999), ("complete", False),
    ("stream", "stderr"), ("content", "too many characters")])
def test_changed_retrieval_receipt_is_rejected_without_refunding_read_quota(tmp_path, field, value):
    reader, record, _, store, _ = fixture(tmp_path)
    original = reader.channel.call

    def tampered(name, args):
        result = original(name, args)
        result.output[field] = value
        return result

    reader.channel.call = tampered
    with pytest.raises(JobError, match="artifact_receipt_mismatch"):
        reader.read(record, stream="stdout", offset=0, limit=3)
    assert store.quota().queries == 1 and store.quota().returned_chars == 3


def test_missing_or_unbound_result_cannot_create_a_free_historical_read(tmp_path):
    reader, record, result, store, requests = fixture(tmp_path)
    result.output["artifact_refs"] = []
    with pytest.raises(JobError, match="result_receipt_mismatch"):
        reader.read(record, stream="stdout", offset=0, limit=3)
    record = record.model_copy(update={"outcome_sha256": _digest(result)})
    with pytest.raises(JobError, match="reference_missing"):
        reader.read(record, stream="stdout", offset=0, limit=3)
    assert not requests and store.quota().queries == 0
