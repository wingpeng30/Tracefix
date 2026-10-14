from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

from tracefix import ToolCall
from tracefix.artifact_transport import _BoundedReader, _BoundedWriter
from tracefix.artifacts import (
    MAX_SESSION_QUERIES,
    ArtifactError,
    ArtifactStore,
)
from tracefix.process_capture import ProcessCapture
from tracefix.tools.artifacts import (
    ArtifactRecordingTool,
    ReadToolArtifactTool,
    SearchToolArtifactTool,
)
from tracefix.tools.base import BaseTool, ToolResult, ToolSpec


def make_store(root: Path, *, run_id: str = "run-a") -> ArtifactStore:
    return ArtifactStore(
        root / "artifacts",
        run_id=run_id,
        workspace=root / "source",
        secret_values=("local-secret-value",),
    )


def test_artifact_read_search_redacts_and_uses_unicode_offsets(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    content = "前" * 80 + "needle-local-secret-value-🙂-" + "尾" * 80
    ref = store.publish_process(
        call_id="call-1",
        tool_name="run_tests",
        source_identity={"product_diff_sha256_before": "a" * 64},
        stdout=content.encode("utf-8") + b"\xff",
        stderr=b"diagnostic",
        complete=True,
    )

    result = store.search("needle", max_matches=2)
    assert len(result.hits) == 1
    assert result.hits[0].artifact.artifact_id == ref.artifact_id
    assert "local-secret-value" not in result.hits[0].excerpt
    assert "[REDACTED]" in result.hits[0].excerpt

    first = store.read(ref.artifact_id, offset=78, limit=2)
    second = store.read(ref.artifact_id, offset=first.next_offset, limit=8)
    assert first.content == "前" * 2
    assert second.content.startswith("needle-")
    assert second.offset == 80

    read_tool = ReadToolArtifactTool(store)
    tool_result = read_tool.execute(
        ToolCall(
            id="read-1",
            name="read_tool_artifact",
            arguments={"artifact_id": ref.artifact_id, "offset": 0, "limit": 8},
        )
    )
    assert tool_result.success
    assert tool_result.output["evidence_role"] == "historical_tool_output_only"
    invalid = SearchToolArtifactTool(store).execute(
        ToolCall(id="search-2", name="search_tool_artifact", arguments={"query": "secret.*"})
    )
    assert invalid.success
    assert not invalid.output["hits"]
    assert store.quota().queries == 5


def test_artifact_ids_and_quotas_are_run_scoped_and_persist(tmp_path: Path) -> None:
    first = make_store(tmp_path)
    ref = first.publish_process(
        call_id="call-1", tool_name="run_tests", source_identity={},
        stdout=b"stable output", stderr=b"", complete=True,
    )
    first.read(ref.artifact_id, limit=5)
    second = ArtifactStore(
        tmp_path / "artifacts", run_id="run-a", workspace=tmp_path / "source",
        secret_values=("local-secret-value",),
    )
    assert second.quota().queries == 1
    assert second.quota().returned_chars == 5
    foreign = make_store(tmp_path / "other", run_id="run-b")
    with pytest.raises(ArtifactError, match="not available"):
        foreign.read(ref.artifact_id)

    manifest = json.loads((tmp_path / "artifacts" / "manifest.json").read_text())
    manifest["query_count"] = MAX_SESSION_QUERIES
    (tmp_path / "artifacts" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    exhausted = ArtifactStore(
        tmp_path / "artifacts", run_id="run-a", workspace=tmp_path / "source",
        secret_values=("local-secret-value",),
    )
    with pytest.raises(ArtifactError, match="query quota"):
        exhausted.read(ref.artifact_id)


def test_artifact_tampering_is_rejected_on_reopen(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ref = store.publish_process(
        call_id="call-1", tool_name="run_tests", source_identity={},
        stdout=b"original", stderr=b"", complete=True,
    )
    view = next((store.objects / ref.artifact_id).glob("view-stdout.txt"))
    view.write_text("changed", encoding="utf-8")
    with pytest.raises(ArtifactError, match="integrity"):
        make_store(tmp_path)


def test_manifest_aggregate_digest_cannot_be_rewritten_independently(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.publish_process(
        call_id="call-1", tool_name="run_tests", source_identity={},
        stdout=b"verified output", stderr=b"", complete=True,
    )
    path = store.manifest_path
    manifest = json.loads(path.read_text(encoding="utf-8"))
    entry = next(iter(manifest["artifacts"].values()))
    entry["ref"]["raw_sha256"] = "0" * 64
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactError, match="aggregate"):
        make_store(tmp_path)


def test_artifact_archive_roundtrip_restores_content_and_query_quotas(tmp_path: Path) -> None:
    source = make_store(tmp_path)
    ref = source.publish_process(
        call_id="call-1", tool_name="run_tests", source_identity={"source": "sha"},
        stdout=("中间needle🙂" * 100).encode(), stderr=b"stderr", complete=True,
    )
    source.read(ref.artifact_id, offset=2, limit=9)
    archive = io.BytesIO()
    receipt = source.export_archive(archive)

    target = make_store(tmp_path / "restored")
    restored = target.import_archive(io.BytesIO(archive.getvalue()))
    assert receipt["artifact_count"] == restored["artifact_count"] == 1
    assert target.quota().queries == 1
    page = target.read(ref.artifact_id, offset=2, limit=9)
    assert page.content == "needle🙂中间"


def test_stream_transport_is_bounded_and_roundtrips_receipt(tmp_path: Path) -> None:
    source = make_store(tmp_path)
    source.publish_process(
        call_id="transport", tool_name="run_tests", source_identity={},
        stdout=b"streamed output", stderr=b"", complete=True,
    )
    archive = io.BytesIO()
    receipt = source.export_archive(_BoundedWriter(archive, 1024 * 1024))
    target = make_store(tmp_path / "target")
    imported = target.import_archive(_BoundedReader(io.BytesIO(archive.getvalue()), 1024 * 1024))
    assert imported["manifest_sha256"] == receipt["manifest_sha256"]
    with pytest.raises(ArtifactError, match="transfer limit"):
        _BoundedReader(io.BytesIO(b"too large"), 4).read(10)
    with pytest.raises(ArtifactError, match="transfer limit"):
        _BoundedWriter(io.BytesIO(), 4).write(b"too large")


def test_artifact_archive_rejects_traversal_before_writing(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    manifest = {
        "schema_version": 1,
        "run_id": "run-a",
        "redaction_policy_sha256": store.redaction_policy_sha256,
        "query_count": 0,
        "returned_chars": 0,
        "artifacts": {},
    }
    archive_data = io.BytesIO()
    with tarfile.open(fileobj=archive_data, mode="w") as archive:
        payload = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
        malicious = tarfile.TarInfo("objects/../../escape")
        malicious.size = 4
        archive.addfile(malicious, io.BytesIO(b"evil"))
    with pytest.raises(ArtifactError, match="unsafe"):
        store.import_archive(io.BytesIO(archive_data.getvalue()))
    assert not (tmp_path / "escape").exists()


def test_search_offsets_remain_unicode_offsets_after_casefold_expansion(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ref = store.publish_process(
        call_id="casefold", tool_name="run_tests", source_identity={},
        stdout=("前" * 200 + "ßTARGET后").encode(), stderr=b"", complete=True,
    )
    hit = store.search("target").hits[0]
    assert hit.artifact.artifact_id == ref.artifact_id
    assert hit.offset == 121
    page = store.read(ref.artifact_id, offset=hit.offset, limit=96)
    assert "ßTARGET" in page.content


def test_structured_tool_result_is_archived_without_claiming_unreturned_data(
    tmp_path: Path,
) -> None:
    class LargeResultTool(BaseTool):
        @property
        def spec(self) -> ToolSpec:
            return ToolSpec(name="search_code", description="search", input_schema={
                "type": "object", "properties": {},
            })

        def execute(self, call: ToolCall) -> ToolResult:
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=True,
                output={"matches": [{"content": "A" * 5000 + "MIDDLE_MARKER" + "B" * 5000}]},
            )

    store = make_store(tmp_path)
    wrapped = ArtifactRecordingTool(LargeResultTool(), store)
    call = ToolCall(id="search-large", name="search_code", arguments={})
    result = wrapped.execute(call)
    ref = result.output["artifact_refs"][0]
    page = store.read(ref["artifact_id"], limit=4096)
    assert "MIDDLE_MARKER" not in page.content
    assert store.search("MIDDLE_MARKER").hits
    serialized_parts = [page.content]
    offset = page.next_offset
    while not page.eof:
        page = store.read(ref["artifact_id"], offset=offset, limit=4096)
        serialized_parts.append(page.content)
        offset = page.next_offset
    recovered = json.loads("".join(serialized_parts))
    assert recovered["tool_name"] == "search_code"
    assert recovered["output"]["matches"][0]["content"].endswith("B" * 5000)


def test_process_capture_drains_both_streams_and_preserves_middle_for_search(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    script = (
        "import sys; "
        "sys.stdout.buffer.write(b'A'*50000+b'MID_MARKER'+b'Z'*50000); "
        "sys.stderr.buffer.write(b'E'*70000+b'ERR_MARKER'+b'F'*70000); "
        "sys.stdout.flush(); sys.stderr.flush()"
    )
    result = ProcessCapture(store).run(
        [sys.executable, "-c", script], cwd=tmp_path, env={}, timeout=10,
        call_id="capture-1", tool_name="run_tests", source_identity={"source": "before"},
    )
    assert result.returncode == 0
    assert not result.incomplete
    assert result.artifact is not None
    assert len(result.stdout.encode()) <= 64 * 1024 + 4
    assert store.search("MID_MARKER").hits[0].offset > 40_000
    assert store.search("ERR_MARKER").hits[0].stream == "stderr"


def test_process_capture_timeout_keeps_partial_artifact_and_reaps_process(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = ProcessCapture(store, drain_timeout_seconds=1).run(
    [
        sys.executable,
        "-c",
        "import sys,time; print('started'); sys.stdout.flush(); time.sleep(20)",
    ],
        cwd=tmp_path,
        env={},
        timeout=0.2,
        call_id="timeout-1",
        tool_name="run_tests",
        source_identity={},
    )
    assert result.timed_out
    assert result.incomplete
    assert result.artifact is not None
    assert not result.artifact.complete
    assert "started" in result.stdout


def test_process_capture_drains_when_artifact_quota_is_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    monkeypatch.setattr("tracefix.artifacts.MAX_RUN_ARTIFACTS", 0)
    result = ProcessCapture(store).run(
        [sys.executable, "-c", "print('still drained')"],
        cwd=tmp_path,
        env={},
        timeout=5,
        call_id="quota-1",
        tool_name="run_tests",
        source_identity={},
    )
    assert result.returncode == 0
    assert result.incomplete
    assert result.artifact is None
    assert result.artifact_error == "artifact_quota_exhausted"
    assert "still drained" in result.stdout


def test_process_capture_continues_draining_after_storage_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    original_open = tempfile.TemporaryFile
    failed = False

    def fail_first_spool(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated disk full")
        return original_open(*args, **kwargs)

    monkeypatch.setattr(tempfile, "TemporaryFile", fail_first_spool)
    store = make_store(tmp_path)
    result = ProcessCapture(store).run(
        [
            sys.executable,
            "-c",
            "import sys; print('stdout drained'); print('stderr drained', file=sys.stderr)",
        ],
        cwd=tmp_path,
        env={},
        timeout=5,
        call_id="disk-full",
        tool_name="run_tests",
        source_identity={},
    )
    assert result.returncode == 0
    assert result.incomplete
    assert result.artifact is not None and not result.artifact.complete
    assert result.artifact_error == "capture_storage_write_failed"
    assert "stdout drained" in result.stdout
    assert "stderr drained" in result.stderr


def test_artifact_store_disk_failure_never_returns_a_false_locator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)

    def fail_manifest(_manifest: dict) -> None:
        raise OSError("simulated artifact-store disk full")

    monkeypatch.setattr(store, "_save_manifest", fail_manifest)
    result = ProcessCapture(store).run(
        [sys.executable, "-c", "print('output remains visible after publish failure')"],
        cwd=tmp_path,
        env={},
        timeout=5,
        call_id="publish-disk-full",
        tool_name="run_tests",
        source_identity={},
    )
    assert result.returncode == 0
    assert result.incomplete
    assert result.artifact is None
    assert result.artifact_error == "artifact_publish_failed:OSError"
    assert "output remains visible" in result.stdout
    assert store.refs() == ()
    assert list(store.objects.iterdir()) == []
