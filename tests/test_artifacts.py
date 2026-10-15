from __future__ import annotations

import io
import json
import sys
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix import ToolCall
from tracefix.artifact_transport import (
    _BoundedReader,
    _BoundedWriter,
)
from tracefix.artifact_transport import (
    main as artifact_transport_main,
)
from tracefix.artifacts import (
    MAX_ARTIFACT_BYTES,
    MAX_SESSION_QUERIES,
    MAX_SESSION_RETURNED_CHARS,
    ArtifactError,
    ArtifactRef,
    ArtifactStore,
    _bounded_text_bytes,
    _original_character_offset,
    _validate_relative,
)
from tracefix.process_capture import ProcessCapture, _Budget, _Preview
from tracefix.tools.artifacts import (
    ArtifactRecordingTool,
    ReadToolArtifactTool,
    SearchToolArtifactTool,
)
from tracefix.tools.base import BaseTool, SkillCatalogEntry, ToolResult, ToolSpec


def make_store(root: Path, *, run_id: str = "run-a") -> ArtifactStore:
    return ArtifactStore(
        root / "artifacts",
        run_id=run_id,
        workspace=root / "source",
        secret_values=("local-secret-value",),
    )


def _archive_members(data: bytes) -> list[tuple[tarfile.TarInfo, bytes]]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        return [
            (member, archive.extractfile(member).read() if member.isfile() else b"")
            for member in archive.getmembers()
        ]


def _pack_archive(members: list[tuple[tarfile.TarInfo, bytes]]) -> bytes:
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w") as archive:
        for original, payload in members:
            member = tarfile.TarInfo(original.name)
            member.type = original.type
            member.linkname = original.linkname
            member.mode = original.mode
            member.size = len(payload) if member.isfile() else 0
            archive.addfile(member, io.BytesIO(payload) if member.isfile() else None)
    return result.getvalue()


def _capture_archive(root: Path, *, artifact_count: int = 1) -> tuple[bytes, list[str]]:
    source = make_store(root)
    artifact_ids = []
    for index in range(artifact_count):
        ref = source.publish_process(
            call_id=f"archive-{index}",
            tool_name="run_tests",
            source_identity={},
            stdout=f"archive payload {index}".encode(),
            stderr=b"",
            complete=True,
        )
        artifact_ids.append(ref.artifact_id)
    archive = io.BytesIO()
    source.export_archive(archive)
    return archive.getvalue(), artifact_ids


def _rewrite_archive_manifest(
    members: list[tuple[tarfile.TarInfo, bytes]],
    mutate,
) -> list[tuple[tarfile.TarInfo, bytes]]:
    header, payload = members[0]
    assert header.name == "manifest.json"
    manifest = json.loads(payload)
    mutate(manifest)
    updated = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return [(header, updated), *members[1:]]


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
        call_id="call-1",
        tool_name="run_tests",
        source_identity={},
        stdout=b"stable output",
        stderr=b"",
        complete=True,
    )
    first.read(ref.artifact_id, limit=5)
    second = ArtifactStore(
        tmp_path / "artifacts",
        run_id="run-a",
        workspace=tmp_path / "source",
        secret_values=("local-secret-value",),
    )
    assert second.quota().queries == 1
    assert second.quota().returned_chars == 5
    foreign = make_store(tmp_path / "other", run_id="run-b")
    with pytest.raises(ArtifactError, match="not available"):
        foreign.read(ref.artifact_id)

    manifest = json.loads((tmp_path / "artifacts" / "manifest.json").read_text())
    manifest["query_count"] = MAX_SESSION_QUERIES
    (tmp_path / "artifacts" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    exhausted = ArtifactStore(
        tmp_path / "artifacts",
        run_id="run-a",
        workspace=tmp_path / "source",
        secret_values=("local-secret-value",),
    )
    with pytest.raises(ArtifactError, match="query quota"):
        exhausted.read(ref.artifact_id)


def test_artifact_tampering_is_rejected_on_reopen(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ref = store.publish_process(
        call_id="call-1",
        tool_name="run_tests",
        source_identity={},
        stdout=b"original",
        stderr=b"",
        complete=True,
    )
    view = next((store.objects / ref.artifact_id).glob("view-stdout.txt"))
    view.write_text("changed", encoding="utf-8")
    with pytest.raises(ArtifactError, match="integrity"):
        make_store(tmp_path)


def test_manifest_aggregate_digest_cannot_be_rewritten_independently(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.publish_process(
        call_id="call-1",
        tool_name="run_tests",
        source_identity={},
        stdout=b"verified output",
        stderr=b"",
        complete=True,
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
        call_id="call-1",
        tool_name="run_tests",
        source_identity={"source": "sha"},
        stdout=("中间needle🙂" * 100).encode(),
        stderr=b"stderr",
        complete=True,
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
        call_id="transport",
        tool_name="run_tests",
        source_identity={},
        stdout=b"streamed output",
        stderr=b"",
        complete=True,
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


def test_stream_transport_exact_limit_and_short_write_are_fail_closed() -> None:
    reader = _BoundedReader(io.BytesIO(b"four"), 4)
    assert reader.read() == b"four"
    assert reader.read() == b""
    with pytest.raises(ArtifactError, match="transfer limit"):
        _BoundedReader(io.BytesIO(b"fives"), 4).read()

    class ShortWriter:
        def write(self, data: bytes) -> int:
            return len(data) - 1

    with pytest.raises(ArtifactError, match="incomplete"):
        _BoundedWriter(ShortWriter(), 8).write(b"payload")


@pytest.mark.parametrize("streaming", [False, True])
def test_artifact_transport_cli_roundtrips_a_bounded_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    from tracefix import artifact_transport

    root = tmp_path / "source"
    root.mkdir()
    workspace = root / "workspace"
    workspace.mkdir()
    run_id = "transport-cli"
    store_root = root / "artifacts"
    environment_path = root / "environment.json"
    environment_path.write_text('{"PROJECT_MODE":"safe"}', encoding="utf-8")
    source = ArtifactStore(
        store_root,
        run_id=run_id,
        workspace=workspace,
        secret_values=("safe",),
        streaming_enabled=streaming,
    )
    ref = source.publish_process(
        call_id="cli-export",
        tool_name="run_tests",
        source_identity={},
        stdout=b"safe",
        stderr=b"",
        complete=True,
    )
    common = [
        "--root",
        str(store_root),
        "--workspace",
        str(workspace),
        "--environment-json",
        str(environment_path),
        "--run-id",
        run_id,
        *(["--streaming-enabled"] if streaming else []),
    ]

    archive = io.BytesIO()
    export_receipt = io.StringIO()
    monkeypatch.setattr(
        artifact_transport.sys,
        "stdout",
        SimpleNamespace(buffer=archive, write=archive.write, flush=archive.flush),
    )
    monkeypatch.setattr(artifact_transport.sys, "stderr", export_receipt)
    assert artifact_transport_main(["export", *common]) == 0
    exported = json.loads(export_receipt.getvalue())
    assert exported["artifact_count"] == 1
    assert archive.getvalue()

    target_root = root / "restored-artifacts"
    monkeypatch.setattr(
        artifact_transport.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(archive.getvalue())),
    )
    import_receipt = io.StringIO()
    monkeypatch.setattr(artifact_transport.sys, "stdout", import_receipt)
    assert (
        artifact_transport_main(
            [
                "import",
                "--root",
                str(target_root),
                *common[2:],
            ]
        )
        == 0
    )
    imported = json.loads(import_receipt.getvalue())
    assert imported["manifest_sha256"] == exported["manifest_sha256"]
    target = ArtifactStore(
        target_root,
        run_id=run_id,
        workspace=workspace,
        secret_values=("safe",),
        streaming_enabled=streaming,
    )
    assert target.refs()[0].artifact_id == ref.artifact_id
    assert target.read(ref.artifact_id).content == "[REDACTED]"


def test_artifact_transport_cli_rejects_credential_environment_overrides(
    tmp_path: Path,
) -> None:
    from tracefix.artifact_transport import main

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment_path = tmp_path / "environment.json"
    environment_path.write_text('{"API_KEY":"fixture-only"}', encoding="utf-8")
    with pytest.raises(ValueError, match="cannot set credentials"):
        main(
            [
                "export",
                "--root",
                str(tmp_path / "artifacts"),
                "--workspace",
                str(workspace),
                "--environment-json",
                str(environment_path),
                "--run-id",
                "bad-environment",
            ]
        )


def test_artifact_tool_adapters_reject_bad_reads_and_preserve_recording_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    ref = store.publish_process(
        call_id="tool-errors",
        tool_name="run_tests",
        source_identity={},
        stdout=b"output",
        stderr=b"",
        complete=True,
    )
    monkeypatch.setattr(
        store,
        "read",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ArtifactError("artifact view failed integrity validation")
        ),
    )
    rejected_read = ReadToolArtifactTool(store).execute(
        ToolCall(
            id="bad-read",
            name="read_tool_artifact",
            arguments={"artifact_id": ref.artifact_id},
        )
    )
    assert not rejected_read.success
    assert rejected_read.error == "artifact view failed integrity validation"

    rejected_search = SearchToolArtifactTool(store).execute(
        ToolCall(
            id="bad-search",
            name="search_tool_artifact",
            arguments={"query": "output", "regex": ".*"},
        )
    )
    assert not rejected_search.success
    assert rejected_search.error == "invalid artifact search request"

    class RecordingSearchTool(BaseTool):
        @property
        def spec(self) -> ToolSpec:
            return ToolSpec(
                name="search_code",
                description="search",
                input_schema={"type": "object", "properties": {}},
            )

        @property
        def skill_catalog(self) -> tuple[SkillCatalogEntry, ...]:
            return (
                SkillCatalogEntry(
                    name="test-skill",
                    description="fixture skill",
                    version="1",
                    sha256="a" * 64,
                ),
            )

        def prepare(self, call: ToolCall) -> str:
            return call.id

        def execute(self, call: ToolCall) -> ToolResult:
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=True,
                output={"matches": []},
                metadata={"source": "test-tool"},
            )

    inner = RecordingSearchTool()
    recorder = ArtifactRecordingTool(inner, store)
    assert recorder.spec.name == "search_code"
    assert recorder.skill_catalog[0].name == "test-skill"
    assert recorder.prepare(ToolCall(id="prepared", name="search_code", arguments={})) == (
        "prepared"
    )

    def fail_publish(*_args, **_kwargs):
        raise ArtifactError("artifact store quota is exhausted")

    monkeypatch.setattr(store, "publish_process", fail_publish)
    result = recorder.execute(ToolCall(id="record", name="search_code", arguments={}))
    assert result.success
    assert result.output == {"matches": []}
    assert result.metadata == {
        "source": "test-tool",
        "artifact_recording_error": "artifact_quota_exhausted",
    }

    checkpoint_diff = recorder.execute(
        ToolCall(
            id="checkpoint-diff",
            name="get_git_diff",
            arguments={},
        )
    )
    assert checkpoint_diff.success
    assert checkpoint_diff.metadata == {"source": "test-tool"}


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
        call_id="casefold",
        tool_name="run_tests",
        source_identity={},
        stdout=("前" * 200 + "ßTARGET后").encode(),
        stderr=b"",
        complete=True,
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
            return ToolSpec(
                name="search_code",
                description="search",
                input_schema={
                    "type": "object",
                    "properties": {},
                },
            )

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
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={},
        timeout=10,
        call_id="capture-1",
        tool_name="run_tests",
        source_identity={"source": "before"},
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


@pytest.mark.parametrize(
    "identity",
    [
        {"": "value"},
        {"k" * 65: "value"},
        {"key": "v" * 1025},
        {"bad\x00key": "value"},
        {"key": "bad\x00value"},
        {f"key-{index}": "value" for index in range(17)},
    ],
)
def test_artifact_reference_rejects_unbounded_or_invalid_source_identity(
    tmp_path: Path,
    identity,
) -> None:
    store = ArtifactStore(tmp_path / "artifact-store", run_id="ref-only")
    ref = store.publish_process(
        call_id="ref",
        tool_name="run_tests",
        source_identity={},
        stdout=b"x",
        stderr=b"",
        complete=True,
    )
    value = ref.model_dump(mode="json")
    value["source_identity"] = identity
    with pytest.raises(ValueError, match="source identity"):
        ArtifactRef.model_validate(value)


@pytest.mark.parametrize(
    "complete,integrity",
    [(True, "incomplete"), (False, "verified")],
)
def test_artifact_reference_rejects_contradictory_integrity_state(
    tmp_path: Path,
    complete: bool,
    integrity: str,
) -> None:
    ref = make_store(tmp_path).publish_process(
        call_id="ref",
        tool_name="run_tests",
        source_identity={},
        stdout=b"x",
        stderr=b"",
        complete=True,
    )
    value = ref.model_dump(mode="json")
    value["complete"] = complete
    value["integrity"] = integrity
    with pytest.raises(ValueError, match="completeness and integrity"):
        ArtifactRef.model_validate(value)


def test_redaction_view_covers_fixed_secret_formats_but_raw_capture_remains_auditable(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    raw = (
        b"Authorization: Bearer auth-secret-123456\n"
        b"bearer standalone-secret-123456\n"
        b"-----BEGIN RSA PRIVATE KEY-----\nprivate-key-body\n"
        b"-----END RSA PRIVATE KEY-----\n"
        b"AKIA1234567890ABCDEF\n"
        b"api_key=assigned-secret-123456\n"
        b"configured local-secret-value\n"
    )
    ref = store.publish_process(
        call_id="redaction",
        tool_name="run_tests",
        source_identity={},
        stdout=raw,
        stderr=b"",
        complete=True,
    )

    view = store.read(ref.artifact_id, limit=4096).content
    raw_path = store.objects / ref.artifact_id / "raw-stdout.bin"
    assert raw_path.read_bytes() == raw
    for secret in (
        "auth-secret-123456",
        "standalone-secret-123456",
        "private-key-body",
        "AKIA1234567890ABCDEF",
        "assigned-secret-123456",
        "local-secret-value",
    ):
        assert secret not in view
    assert view.count("[REDACTED]") >= 6


def test_view_byte_quota_truncates_only_at_utf8_boundaries_and_marks_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tracefix.artifacts as artifacts

    monkeypatch.setattr(artifacts, "MAX_RUN_BYTES", 13)
    store = ArtifactStore(
        tmp_path / "artifacts",
        run_id="small",
        workspace=tmp_path / "source",
        secret_values=("x",),
    )
    ref = store.publish_process(
        call_id="bounded-view",
        tool_name="run_tests",
        source_identity={},
        stdout=b"x" * 8,
        stderr=b"",
        complete=True,
    )

    assert not ref.complete
    assert ref.integrity == "incomplete"
    assert ref.raw_bytes + ref.view_bytes == 13
    assert store.read(ref.artifact_id, limit=4096).content.encode() == b"[REDA"
    assert _bounded_text_bytes("中", 2) == b""
    assert _bounded_text_bytes("中", 3) == "中".encode()


@pytest.mark.parametrize(
    "manifest_text",
    [
        "not json",
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "run-a",
                "artifacts": {},
                "query_count": True,
                "returned_chars": 0,
                "redaction_policy_sha256": "0" * 64,
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "run-a",
                "artifacts": {},
                "query_count": 0,
                "returned_chars": -1,
                "redaction_policy_sha256": "0" * 64,
            }
        ),
    ],
)
def test_corrupt_or_malformed_manifest_fails_closed(tmp_path: Path, manifest_text: str) -> None:
    store = make_store(tmp_path)
    store.manifest_path.write_text(manifest_text, encoding="utf-8")
    with pytest.raises(ArtifactError, match="corrupt|invalid"):
        make_store(tmp_path)


def test_manifest_atomic_replace_failure_preserves_old_manifest_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    before = store.manifest_path.read_bytes()

    def fail_replace(*_args) -> None:
        raise OSError("simulated atomic replacement failure")

    monkeypatch.setattr("tracefix.artifacts.os.replace", fail_replace)
    with pytest.raises(OSError, match="atomic replacement"):
        store._save_manifest({**store._load_manifest(), "returned_chars": 1})

    assert store.manifest_path.read_bytes() == before
    assert not list(store.root.glob(".manifest-*.tmp"))


@pytest.mark.parametrize(
    "fault",
    [
        "top-level-identity",
        "too-many-artifacts",
        "key-mismatch",
        "wrong-run-ref",
        "incomplete-files",
        "wrong-receipt-shape",
        "wrong-digest",
        "wrong-size-type",
        "wrong-raw-byte-count",
        "wrong-view-byte-count",
        "wrong-redaction-id",
        "too-much-storage",
        "too-many-queries",
        "too-many-returned-chars",
        "extra-object",
        "missing-object",
    ],
)
def test_manifest_inventory_rejects_tampered_receipts_and_quotas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    import tracefix.artifacts as artifacts

    store = make_store(tmp_path)
    ref = store.publish_process(
        call_id="inventory",
        tool_name="run_tests",
        source_identity={},
        stdout=b"stdout",
        stderr=b"stderr",
        complete=True,
    )
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    entry = manifest["artifacts"][ref.artifact_id]
    receipt = entry["files"]["raw_stdout"]

    if fault == "top-level-identity":
        manifest["run_id"] = "another-run"
    elif fault == "too-many-artifacts":
        monkeypatch.setattr(artifacts, "MAX_RUN_ARTIFACTS", 0)
    elif fault == "key-mismatch":
        manifest["artifacts"] = {"f" * 32: entry}
    elif fault == "wrong-run-ref":
        entry["ref"]["run_id"] = "another-run"
    elif fault == "incomplete-files":
        entry["files"].pop("raw_stdout")
    elif fault == "wrong-receipt-shape":
        entry["files"]["raw_stdout"] = None
    elif fault == "wrong-digest":
        receipt[1] = "not-a-digest"
    elif fault == "wrong-size-type":
        receipt[2] = True
    elif fault == "wrong-raw-byte-count":
        entry["ref"]["raw_bytes"] += 1
    elif fault == "wrong-view-byte-count":
        entry["ref"]["view_bytes"] += 1
    elif fault == "wrong-redaction-id":
        entry["ref"]["redaction_policy_sha256"] = "0" * 64
    elif fault == "too-much-storage":
        monkeypatch.setattr(artifacts, "MAX_RUN_BYTES", 1)
    elif fault == "too-many-queries":
        manifest["query_count"] = MAX_SESSION_QUERIES + 1
    elif fault == "too-many-returned-chars":
        manifest["returned_chars"] = MAX_SESSION_RETURNED_CHARS + 1
    elif fault == "extra-object":
        (store.objects / "unindexed").mkdir()
    else:
        store._owned_file(entry["files"]["raw_stdout"][0]).unlink()

    with pytest.raises(ArtifactError):
        store._validate_inventory(manifest)


@pytest.mark.parametrize("target", ["root", "objects", "manifest"])
def test_artifact_store_refuses_symlinked_owned_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    root = tmp_path / "artifact-store"
    if target != "root":
        ArtifactStore(root, run_id="run-a")
    blocked = {
        "root": root,
        "objects": root / "objects",
        "manifest": root / "manifest.json",
    }[target]
    original = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == blocked or original(path),
    )

    with pytest.raises(ArtifactError, match="symbolic link"):
        ArtifactStore(root, run_id="run-a")


@pytest.mark.parametrize(
    "path",
    ["../outside", "/absolute", "objects\\outside"],
)
def test_artifact_owned_file_rejects_nonportable_paths(tmp_path: Path, path: str) -> None:
    store = make_store(tmp_path)
    with pytest.raises(ArtifactError, match="unsafe"):
        store._owned_file(path)


def test_artifact_owned_file_rejects_missing_and_linked_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    ref = store.publish_process(
        call_id="owned-file",
        tool_name="run_tests",
        source_identity={},
        stdout=b"body",
        stderr=b"",
        complete=True,
    )
    with pytest.raises(ArtifactError, match="missing"):
        store._owned_file("objects/missing/file.txt")

    blocked = store.objects / ref.artifact_id
    original = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == blocked or original(path),
    )
    with pytest.raises(ArtifactError, match="symbolic link"):
        store._owned_file(f"objects/{ref.artifact_id}/raw-stdout.bin")


@pytest.mark.parametrize(
    "operation,fault",
    [
        ("read", "path"),
        ("read", "content"),
        ("read", "offset"),
        ("search", "path"),
        ("search", "content"),
    ],
)
def test_artifact_retrieval_rechecks_locator_integrity_and_bounds(
    tmp_path: Path,
    operation: str,
    fault: str,
) -> None:
    store = make_store(tmp_path)
    ref = store.publish_process(
        call_id="retrieval-integrity",
        tool_name="run_tests",
        source_identity={},
        stdout=b"searchable payload",
        stderr=b"",
        complete=True,
    )
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    entry = manifest["artifacts"][ref.artifact_id]
    if fault == "path":
        entry["files"]["view_stdout"][0] = "objects/other/view-stdout.txt"
        store.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif fault == "content":
        store._owned_file(entry["files"]["view_stdout"][0]).write_bytes(b"changed")

    with pytest.raises(ArtifactError):
        if operation == "search":
            store.search("searchable")
        elif fault == "offset":
            store.read(ref.artifact_id, offset=100)
        else:
            store.read(ref.artifact_id)

    assert store.quota().queries == 0


def test_artifact_checkpoint_identity_and_archive_member_path_contract(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    identity = store.checkpoint_identity()
    store.verify_checkpoint_identity(identity)
    with pytest.raises(ArtifactError, match="identity or cumulative quota"):
        store.verify_checkpoint_identity({})
    with pytest.raises(ArtifactError, match="not text"):
        _validate_relative(None)


@pytest.mark.parametrize(
    "fault",
    [
        "wrong-run",
        "wrong-redaction",
        "incomplete-inventory",
        "bad-file-receipt",
        "traversal-receipt",
        "inconsistent-byte-count",
        "missing-member",
        "extra-member",
        "duplicate-member",
        "symbolic-link",
        "changed-member",
    ],
)
def test_archive_import_rejects_invalid_identity_receipts_and_members_atomically(
    tmp_path: Path,
    fault: str,
) -> None:
    archive_bytes, artifact_ids = _capture_archive(tmp_path / "source")
    members = _archive_members(archive_bytes)
    manifest = json.loads(members[0][1])
    artifact = manifest["artifacts"][artifact_ids[0]]
    file_name = "objects/../../escape"

    if fault == "wrong-run":
        manifest["run_id"] = "another-run"
        members = _rewrite_archive_manifest(members, lambda value: value.update(manifest))
    elif fault == "wrong-redaction":
        manifest["redaction_policy_sha256"] = "0" * 64
        members = _rewrite_archive_manifest(members, lambda value: value.update(manifest))
    elif fault == "incomplete-inventory":
        artifact["files"].pop("raw_stdout")
        members = _rewrite_archive_manifest(
            members, lambda value: value.update(artifacts=manifest["artifacts"])
        )
    elif fault == "bad-file-receipt":
        artifact["files"]["raw_stdout"][1] = "not-a-sha256"
        members = _rewrite_archive_manifest(
            members, lambda value: value.update(artifacts=manifest["artifacts"])
        )
    elif fault == "traversal-receipt":
        artifact["files"]["raw_stdout"][0] = file_name
        members = _rewrite_archive_manifest(
            members, lambda value: value.update(artifacts=manifest["artifacts"])
        )
    elif fault == "inconsistent-byte-count":
        artifact["files"]["raw_stdout"][2] += 1
        members = _rewrite_archive_manifest(
            members, lambda value: value.update(artifacts=manifest["artifacts"])
        )
    elif fault == "missing-member":
        members.pop()
    elif fault == "extra-member":
        info = tarfile.TarInfo("objects/unexpected.bin")
        members.append((info, b"unexpected"))
    elif fault == "duplicate-member":
        members.append(members[1])
    elif fault == "symbolic-link":
        link = tarfile.TarInfo("objects/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../escape"
        members.append((link, b""))
    else:
        info, payload = members[1]
        members[1] = (info, b"X" + payload[1:])

    target = make_store(tmp_path / "target")
    with pytest.raises(ArtifactError):
        target.import_archive(io.BytesIO(_pack_archive(members)))

    assert target.refs() == ()
    assert list(target.objects.iterdir()) == []
    assert not list(target.root.glob(".restore-*"))
    assert not (tmp_path / "escape").exists()


def test_archive_import_rolls_back_already_moved_objects_after_later_move_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tracefix.artifacts as artifacts

    archive_bytes, _ = _capture_archive(tmp_path / "source", artifact_count=2)
    target = make_store(tmp_path / "target")
    replace = artifacts.os.replace
    moved = 0

    def fail_second_move(source, destination):
        nonlocal moved
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.parent.parent.name.startswith(".restore-") and (
            destination_path.parent == target.objects
        ):
            moved += 1
            if moved == 2:
                raise OSError("second object publication failed")
        return replace(source, destination)

    monkeypatch.setattr(artifacts.os, "replace", fail_second_move)
    with pytest.raises(OSError, match="second object"):
        target.import_archive(io.BytesIO(archive_bytes))

    assert moved == 2
    assert target.refs() == ()
    assert list(target.objects.iterdir()) == []
    assert not list(target.root.glob(".restore-*"))


def test_archive_import_rolls_back_objects_when_manifest_commit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_bytes, _ = _capture_archive(tmp_path / "source")
    target = make_store(tmp_path / "target")
    monkeypatch.setattr(
        target, "_save_manifest", lambda _value: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(OSError, match="disk full"):
        target.import_archive(io.BytesIO(archive_bytes))

    assert target.refs() == ()
    assert list(target.objects.iterdir()) == []
    assert not list(target.root.glob(".restore-*"))


@pytest.mark.parametrize("fault", ["empty", "wrong-name", "directory", "corrupt-json"])
def test_archive_import_requires_a_regular_bounded_json_manifest_header(
    tmp_path: Path,
    fault: str,
) -> None:
    if fault == "empty":
        data = _pack_archive([])
    else:
        name = "other.json" if fault == "wrong-name" else "manifest.json"
        info = tarfile.TarInfo(name)
        if fault == "directory":
            info.type = tarfile.DIRTYPE
            members = [(info, b"")]
        else:
            members = [(info, b"{" if fault == "corrupt-json" else b"{}")]
        data = _pack_archive(members)
    target = make_store(tmp_path / "target")

    with pytest.raises(ArtifactError):
        target.import_archive(io.BytesIO(data))

    assert target.refs() == ()
    assert not list(target.root.glob(".restore-*"))


def test_archive_import_refuses_to_overwrite_a_nonempty_run(tmp_path: Path) -> None:
    archive_bytes, _ = _capture_archive(tmp_path / "source")
    target = make_store(tmp_path / "target")
    target.publish_process(
        call_id="already-here",
        tool_name="run_tests",
        source_identity={},
        stdout=b"existing",
        stderr=b"",
        complete=True,
    )
    before = target.checkpoint_identity()

    with pytest.raises(ArtifactError, match="empty store"):
        target.import_archive(io.BytesIO(archive_bytes))

    assert target.checkpoint_identity() == before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"artifact_id": "../outside"},
        {"offset": -1},
        {"offset": True},
        {"limit": 0},
        {"limit": MAX_ARTIFACT_BYTES + 1},
        {"limit": True},
        {"stream": "combined"},
    ],
)
def test_read_rejects_invalid_ids_streams_and_page_bounds_without_spending_quota(
    tmp_path: Path,
    kwargs: dict,
) -> None:
    store = make_store(tmp_path)
    ref = store.publish_process(
        call_id="bounds",
        tool_name="run_tests",
        source_identity={},
        stdout=b"content",
        stderr=b"",
        complete=True,
    )
    arguments = {"artifact_id": ref.artifact_id, **kwargs}
    with pytest.raises(ArtifactError):
        store.read(**arguments)
    assert store.quota().queries == 0


@pytest.mark.parametrize(
    "query,options",
    [
        ("", {}),
        ("   ", {}),
        ("x" * 257, {}),
        ("x", {"max_matches": 0}),
        ("x", {"max_matches": 21}),
        ("x", {"max_matches": True}),
        ("x", {"excerpt_chars": 31}),
        ("x", {"excerpt_chars": 513}),
    ],
)
def test_search_rejects_invalid_literal_and_limits(
    tmp_path: Path,
    query: str,
    options: dict,
) -> None:
    store = ArtifactStore(tmp_path / "search-store", run_id="search-only")
    with pytest.raises(ArtifactError):
        store.search(query, **options)
    assert store.quota().queries == 0


def test_search_literal_hits_are_bounded_and_report_truncation(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ref = store.publish_process(
        call_id="many-hits",
        tool_name="run_tests",
        source_identity={},
        stdout=("needle!" * 5).encode(),
        stderr=b"needle!",
        complete=True,
    )

    result = store.search("NEEDLE", max_matches=2, excerpt_chars=32)

    assert result.truncated
    assert len(result.hits) == 2
    assert {hit.artifact.artifact_id for hit in result.hits} == {ref.artifact_id}
    assert result.queries_remaining == MAX_SESSION_QUERIES - 1
    assert result.characters_remaining == (
        MAX_SESSION_RETURNED_CHARS - sum(len(hit.excerpt) for hit in result.hits)
    )
    assert store.quota().returned_chars == sum(len(hit.excerpt) for hit in result.hits)


def test_read_and_search_enforce_persisted_character_budget(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ref = store.publish_process(
        call_id="characters",
        tool_name="run_tests",
        source_identity={},
        stdout=b"needle",
        stderr=b"",
        complete=True,
    )
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    manifest["returned_chars"] = MAX_SESSION_RETURNED_CHARS
    store.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = store.search("needle")
    assert result.truncated and result.hits == ()
    with pytest.raises(ArtifactError, match="returned-character quota"):
        store.read(ref.artifact_id, limit=1)
    assert store.quota().returned_chars == MAX_SESSION_RETURNED_CHARS


def test_store_identity_requires_an_external_root_and_rejects_orphan_objects(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    with pytest.raises(ArtifactError, match="outside the source workspace"):
        ArtifactStore(root, run_id="inside", workspace=root)
    with pytest.raises(ArtifactError, match="invalid artifact run identity"):
        ArtifactStore(tmp_path / "bad-run", run_id="")

    orphaned = tmp_path / "orphaned"
    (orphaned / "objects").mkdir(parents=True)
    (orphaned / "objects" / "unindexed").mkdir()
    with pytest.raises(ArtifactError, match="without a manifest"):
        ArtifactStore(orphaned, run_id="orphaned")
    assert (orphaned / "objects" / "unindexed").is_dir()


def test_store_reopen_rejects_run_or_redaction_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tracefix.artifacts as artifacts

    ArtifactStore(tmp_path / "artifacts", run_id="first")
    with pytest.raises(ArtifactError, match="another run"):
        ArtifactStore(tmp_path / "artifacts", run_id="second")

    monkeypatch.setattr(artifacts, "_REDACTION_VERSION", "tracefix-redaction-test")
    with pytest.raises(ArtifactError, match="redaction policy changed"):
        ArtifactStore(tmp_path / "artifacts", run_id="first")


def test_artifact_capture_limits_and_object_count_quota_are_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tracefix.artifacts as artifacts

    store = make_store(tmp_path)
    with pytest.raises(ArtifactError, match="artifact limit"):
        store.publish_process(
            call_id="too-large",
            tool_name="run_tests",
            source_identity={},
            stdout=b"x" * (MAX_ARTIFACT_BYTES + 1),
            stderr=b"",
            complete=True,
        )
    monkeypatch.setattr(artifacts, "MAX_RUN_ARTIFACTS", 1)
    store.publish_process(
        call_id="first",
        tool_name="run_tests",
        source_identity={},
        stdout=b"one",
        stderr=b"",
        complete=True,
    )
    assert store.capture_budget() == 0
    with pytest.raises(ArtifactError, match="count quota"):
        store.publish_process(
            call_id="second",
            tool_name="run_tests",
            source_identity={},
            stdout=b"two",
            stderr=b"",
            complete=True,
        )
    assert len(store.refs()) == 1


def test_character_offset_mapping_handles_expanded_and_terminal_positions() -> None:
    assert _original_character_offset("ßa", 1) == 0
    assert _original_character_offset("ßa", 2) == 1
    assert _original_character_offset("ßa", 3) == 2


def test_capture_budget_and_capture_preview_preserve_the_bounded_head_and_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = _Preview()
    preview.add(b"a" * (_Preview._MAX + 1))
    preview.add(b"tail")
    assert len(preview.text()) == _Preview._MAX
    assert preview.text().startswith("a" * _Preview._HALF)
    assert preview.text().endswith("tail")

    store = make_store(tmp_path)
    assert store.capture_budget() == MAX_ARTIFACT_BYTES
    import tracefix.artifacts as artifacts

    monkeypatch.setattr(artifacts, "MAX_RUN_BYTES", 100)
    store.publish_process(
        call_id="budget",
        tool_name="run_tests",
        source_identity={},
        stdout=b"x" * 10,
        stderr=b"y" * 10,
        complete=True,
    )
    assert store.capture_budget() == 30


@pytest.mark.parametrize("failure", ["short-write", "write-error"])
def test_capture_budget_marks_partial_or_failed_spool_incomplete(failure: str) -> None:
    class ShortWriter:
        def write(self, data: bytes) -> int:
            if failure == "write-error":
                raise OSError("disk full")
            return len(data) - 1

    budget = _Budget(8)
    budget.take(b"payload", ShortWriter())
    assert budget.exhausted
    assert budget.write_failed
    assert budget.remaining == 0


def test_capture_budget_keeps_only_remaining_bytes_after_exhaustion() -> None:
    output = io.BytesIO()
    budget = _Budget(4)
    budget.take(b"abc", output)
    budget.take(b"def", output)
    budget.take(b"ignored", output)

    assert output.getvalue() == b"abcd"
    assert budget.exhausted
    assert not budget.write_failed
    assert budget.remaining == 0


@pytest.mark.parametrize("taskkill_failure", [None, OSError("taskkill unavailable")])
def test_process_capture_terminates_owned_descendants_on_windows(
    monkeypatch: pytest.MonkeyPatch,
    taskkill_failure: OSError | None,
) -> None:
    import tracefix.process_capture as process_capture

    class RunningProcess:
        pid = 7331

        def __init__(self) -> None:
            self.returncode = None
            self.killed = False

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

    process = RunningProcess()
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs) -> SimpleNamespace:
        calls.append(command)
        if taskkill_failure is not None:
            raise taskkill_failure
        return SimpleNamespace(returncode=0)

    if sys.platform == "win32":
        monkeypatch.setattr("tracefix.process_capture.subprocess.run", run)
    else:
        monkeypatch.setattr("tracefix.process_capture.os.killpg", lambda *_args: None)

    process_capture.ProcessCapture._terminate(process, include_descendants=True)

    assert process.killed
    if sys.platform == "win32":
        assert calls == [["taskkill", "/PID", "7331", "/T", "/F"]]


def test_process_capture_preserves_nonzero_status_and_propagates_start_callback_failure(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    result = ProcessCapture(store).run(
        [sys.executable, "-c", "print('failed tests'); raise SystemExit(3)"],
        cwd=tmp_path,
        env={},
        timeout=5,
        call_id="test-failure",
        tool_name="run_tests",
        source_identity={},
    )
    assert result.returncode == 3
    assert not result.incomplete
    assert result.artifact is not None and result.artifact.complete

    def fail_callback() -> None:
        raise RuntimeError("bridge event channel failed")

    with pytest.raises(RuntimeError, match="event channel"):
        ProcessCapture(store).run(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            cwd=tmp_path,
            env={},
            timeout=10,
            call_id="callback-failure",
            tool_name="run_tests",
            source_identity={},
            on_started=fail_callback,
        )


@pytest.mark.parametrize("failure", ["oserror", "valueerror"])
def test_process_capture_reports_pipe_reader_failures_without_losing_process_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    store = make_store(tmp_path)

    class FailingPipe:
        def read(self, _size: int) -> bytes:
            if failure == "oserror":
                raise OSError("pipe disconnected")
            raise ValueError("pipe decoder failed")

    class EmptyPipe:
        def read(self, _size: int) -> bytes:
            return b""

    class FinishedProcess:
        pid = 8123
        stdout = FailingPipe()
        stderr = EmptyPipe()
        returncode = None

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = 0
            return 0

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    process = FinishedProcess()
    monkeypatch.setattr("tracefix.process_capture.subprocess.Popen", lambda *_a, **_kw: process)

    result = ProcessCapture(store).run(
        ["fake-test-process"],
        cwd=tmp_path,
        env={},
        timeout=5,
        call_id=f"reader-{failure}",
        tool_name="run_tests",
        source_identity={},
    )

    assert result.returncode == 0
    assert result.incomplete
    assert result.artifact is not None and not result.artifact.complete
    assert result.reader_error == ("storage_write_failed" if failure == "oserror" else "ValueError")


def test_process_capture_closes_pipes_after_a_drain_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    started = threading.Event()
    released = threading.Event()

    class WaitingPipe:
        def __init__(self, *, fail_close: bool = False) -> None:
            self.fail_close = fail_close

        def read(self, _size: int) -> bytes:
            started.set()
            released.wait(2)
            return b""

        def close(self) -> None:
            released.set()
            if self.fail_close:
                raise OSError("already closed")

    class FinishedProcess:
        pid = 8124
        stdout = WaitingPipe(fail_close=True)
        stderr = WaitingPipe()
        returncode = None

        def wait(self, timeout: float | None = None) -> int:
            assert started.wait(1)
            self.returncode = 0
            return 0

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    process = FinishedProcess()
    monkeypatch.setattr("tracefix.process_capture.subprocess.Popen", lambda *_a, **_kw: process)
    terminated: list[bool] = []

    def terminate(_process, *, include_descendants: bool = False) -> None:
        terminated.append(include_descendants)

    monkeypatch.setattr(ProcessCapture, "_terminate", staticmethod(terminate))
    result = ProcessCapture(store, drain_timeout_seconds=0.01).run(
        ["fake-test-process"],
        cwd=tmp_path,
        env={},
        timeout=5,
        call_id="drain-deadline",
        tool_name="run_tests",
        source_identity={},
    )

    assert result.returncode == 0
    assert result.incomplete
    assert result.reader_error == "drain_timeout"
    assert terminated == [True]
    assert released.is_set()


@pytest.mark.parametrize("kill_group_fails", [False, True])
def test_process_capture_posix_termination_reaps_process_group(
    monkeypatch: pytest.MonkeyPatch,
    kill_group_fails: bool,
) -> None:
    import tracefix.process_capture as process_capture

    class RunningProcess:
        pid = 8125
        returncode = None

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode or 0

    process = RunningProcess()
    calls: list[tuple[int, int]] = []

    def killpg(pid: int, signal_number: int) -> None:
        calls.append((pid, signal_number))
        if kill_group_fails:
            raise ProcessLookupError
        process.returncode = 0

    monkeypatch.setattr(
        process_capture,
        "os",
        SimpleNamespace(name="posix", killpg=killpg),
    )
    monkeypatch.setattr(process_capture, "signal", SimpleNamespace(SIGKILL=9))
    ProcessCapture._terminate(process)

    assert calls == [(process.pid, 9)]
    assert process.returncode in {0, -9}


def test_process_capture_classifies_publish_quota_failure_without_rerunning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    calls = 0

    def publish(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise ArtifactError("artifact storage quota is exhausted")

    monkeypatch.setattr(store, "publish_process", publish)
    result = ProcessCapture(store).run(
        [sys.executable, "-c", "print('captured once')"],
        cwd=tmp_path,
        env={},
        timeout=5,
        call_id="publish-quota",
        tool_name="run_tests",
        source_identity={},
    )

    assert calls == 1
    assert result.returncode == 0
    assert result.incomplete
    assert result.artifact is None
    assert result.artifact_error == "artifact_quota_exhausted"
    assert "captured once" in result.stdout


@pytest.mark.parametrize("timeout", [0, -1])
def test_process_capture_rejects_nonpositive_timeouts(
    tmp_path: Path,
    timeout: float,
) -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        ProcessCapture(make_store(tmp_path)).run(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            env={},
            timeout=timeout,
            call_id="invalid-timeout",
            tool_name="run_tests",
            source_identity={},
        )


def test_process_capture_rejects_nonpositive_drain_deadlines(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="drain timeout must be positive"):
        ProcessCapture(make_store(tmp_path), drain_timeout_seconds=0)
