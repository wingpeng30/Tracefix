from __future__ import annotations

import hashlib
import json

import pytest

from tracefix.artifacts import ArtifactStore
from tracefix.checkpoint import CheckpointError
from tracefix.jobs.artifact_seed import JobArtifactChain, JobArtifactSeed


def seed(root, store, sequence):
    batch = f"{sequence:08d}-" + "a" * 32
    path = root / "docker-checkpoints" / f"{batch}.artifacts.tar"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as output:
        receipt = store.export_archive(output)
    return JobArtifactSeed.from_snapshot(root, {
        "batch_id": batch, "artifacts_enabled": True,
        "artifact_archive": path.relative_to(root).as_posix(),
        "artifact_archive_bytes": path.stat().st_size,
        "artifact_archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "artifact_receipt": receipt, "streaming_artifacts": True, "artifact_run_id": "r1",
        "unrelated_large_source_metadata": "not part of the handoff",
    })


def test_serial_artifact_handoff_retains_old_objects_and_cumulative_reads(tmp_path):
    store = ArtifactStore(tmp_path / "store", run_id="r1", streaming_enabled=True)
    first = store.publish_process(call_id="first", tool_name="run_tests", source_identity={},
        stdout=b"first", stderr=b"", complete=True)
    store.read(first.artifact_id, limit=3)
    chain = JobArtifactChain("r1")
    one = seed(tmp_path, store, 1)
    assert "unrelated_large_source_metadata" not in one.snapshot
    chain.advance(one)
    store.publish_process(call_id="second", tool_name="run_tests", source_identity={},
        stdout=b"second", stderr=b"", complete=True)
    store.read(first.artifact_id, limit=2)
    two = seed(tmp_path, store, 2)
    chain.advance(two)
    assert chain.seed == two
    assert two.snapshot["artifact_receipt"]["queries"] == 2
    restored = ArtifactStore(tmp_path / "fresh", run_id="r1", streaming_enabled=True)
    with (tmp_path / two.snapshot["artifact_archive"]).open("rb") as source:
        restored.import_archive(source)
    assert len(restored.refs()) == 2 and restored.quota().returned_chars == 5


@pytest.mark.parametrize("tamper", ["rewrite", "drop", "query_count", "returned_chars"])
def test_validly_hashed_handoff_cannot_rewrite_history_or_reset_quota(tmp_path, tamper):
    store = ArtifactStore(tmp_path / "store", run_id="r1", streaming_enabled=True)
    ref = store.publish_process(call_id="first", tool_name="run_tests", source_identity={},
        stdout=b"first", stderr=b"", complete=True)
    store.read(ref.artifact_id, limit=3)
    chain = JobArtifactChain("r1")
    first = seed(tmp_path, store, 1)
    chain.advance(first)
    payload = json.loads(store.manifest_path.read_bytes())
    if tamper == "rewrite":
        payload["artifacts"][ref.artifact_id]["ref"]["source_identity"] = {"rewritten": "changed"}
    elif tamper == "drop":
        store = ArtifactStore(tmp_path / "replacement", run_id="r1", streaming_enabled=True)
        payload = json.loads(store.manifest_path.read_bytes())
        payload.update(query_count=1, returned_chars=3)
    else:
        payload[tamper] = 0
    store.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="rewrites history|quota|inventory"):
        chain.advance(seed(tmp_path, store, 2))
    assert chain.seed == first


def test_changed_archive_never_advances_handoff_pointer(tmp_path):
    store = ArtifactStore(tmp_path / "store", run_id="r1", streaming_enabled=True)
    chain = JobArtifactChain("r1")
    value = seed(tmp_path, store, 1)
    path = tmp_path / value.snapshot["artifact_archive"]
    path.write_bytes(path.read_bytes() + b"unexpected")
    with pytest.raises(CheckpointError, match="size"):
        chain.advance(value)
    assert chain.seed is None
