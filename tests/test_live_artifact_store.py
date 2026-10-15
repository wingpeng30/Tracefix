from __future__ import annotations

import io

import pytest

from tracefix import artifacts
from tracefix.artifacts import ArtifactError, ArtifactStore


def test_live_output_reserves_shared_space_and_seals_without_changing_view(tmp_path):
    store = ArtifactStore(tmp_path / "evidence", run_id="r1", streaming_enabled=True)
    reservation = store.reserve_capture()
    assert store.quota().artifacts == 1
    assert store.quota().storage_bytes == 2 * reservation.raw_budget
    assert store.append_capture(reservation.capture_id, stream="stdout",
                                raw=b"raw-secret\n", view="中文🙂\n")
    page = store.read_capture(reservation.capture_id, offset=0, limit=2)
    assert page["content"] == "中文" and page["next_offset"] == 2
    assert not page["final"]
    with pytest.raises(ArtifactError, match="active"):
        store.checkpoint_identity()
    with pytest.raises(ArtifactError, match="active"):
        store.export_archive(io.BytesIO())
    ref = store.publish_capture(reservation.capture_id, call_id="call1", tool_name="run_tests",
                                source_identity={}, complete=True)
    assert store.quota().artifacts == 1
    assert store.quota().storage_bytes == len(b"raw-secret\n") + len("中文🙂\n".encode())
    assert store.read(ref.artifact_id, offset=2).content == "🙂\n"
    assert store.quota().queries == 2
    with pytest.raises(ArtifactError):
        store.append_capture(reservation.capture_id, stream="stdout", raw=b"x", view="x")


def test_crash_keeps_reservation_and_readable_committed_prefix_without_resuming_writer(tmp_path):
    root = tmp_path / "evidence"
    first = ArtifactStore(root, run_id="r1", streaming_enabled=True)
    reservation = first.reserve_capture()
    first.append_capture(reservation.capture_id, stream="stderr", raw=b"hello", view="safe")
    restored = ArtifactStore(root, run_id="r1", streaming_enabled=True)
    assert restored.quota() == first.quota()
    assert restored.read_capture(reservation.capture_id, stream="stderr")["content"] == "safe"
    with pytest.raises(ArtifactError, match="writer"):
        restored.append_capture(reservation.capture_id, stream="stderr", raw=b"more", view="more")
    with pytest.raises(ArtifactError):
        ArtifactStore(root, run_id="other", streaming_enabled=True)
    with pytest.raises(ArtifactError):
        restored.read_capture("../manifest.json")


def test_live_view_tampering_fails_closed_before_return_and_does_not_spend_query(tmp_path):
    store = ArtifactStore(tmp_path / "evidence", run_id="r1", streaming_enabled=True)
    reservation = store.reserve_capture()
    store.append_capture(reservation.capture_id, stream="stdout", raw=b"hello", view="safe")
    path = store.root / "captures" / reservation.capture_id / "view-stdout.txt"
    path.write_text("evil", encoding="utf-8")
    with pytest.raises(ArtifactError, match="integrity"):
        store.read_capture(reservation.capture_id)
    assert store.quota().queries == 0


def test_run_quota_includes_live_reservations_and_utf8_clipping(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", 8)
    monkeypatch.setattr(artifacts, "MAX_RUN_BYTES", 24)
    store = ArtifactStore(tmp_path / "evidence", run_id="r1", streaming_enabled=True)
    first = store.reserve_capture()
    second = store.reserve_capture()
    assert (first.raw_budget, second.raw_budget) == (8, 4)
    assert store.capture_budget() == 0
    assert not store.append_capture(first.capture_id, stream="stdout", raw=b"a" * 20,
                                    view="中文🙂")
    ref = store.publish_capture(first.capture_id, call_id="a", tool_name="test",
                                source_identity={}, complete=True)
    assert not ref.complete and store.read(ref.artifact_id).content == "中文"
    assert store.quota().storage_bytes == 22  # raw8 + view6 + second reservation8


def test_failed_manifest_commit_preserves_old_view_and_seals_incomplete(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "evidence", run_id="r1", streaming_enabled=True)
    capture = store.reserve_capture()
    store.append_capture(capture.capture_id, stream="stdout", raw=b"old", view="old")
    save = store._save_manifest
    monkeypatch.setattr(store, "_save_manifest", lambda value: (_ for _ in ()).throw(
        OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        store.append_capture(capture.capture_id, stream="stdout", raw=b"lost", view="lost")
    monkeypatch.setattr(store, "_save_manifest", save)
    assert store.read_capture(capture.capture_id)["content"] == "old"
    with pytest.raises(ArtifactError, match="failed"):
        store.append_capture(capture.capture_id, stream="stderr", raw=b"retry", view="retry")
    ref = store.publish_capture(capture.capture_id, call_id="a", tool_name="test",
                                source_identity={}, complete=True)
    assert not ref.complete
    assert store.read(ref.artifact_id).content == "old"


def test_failed_final_publish_rolls_back_files_and_does_not_double_charge(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "evidence", run_id="r1", streaming_enabled=True)
    capture = store.reserve_capture()
    store.append_capture(capture.capture_id, stream="stdout", raw=b"hello", view="hello")
    before = store.quota()
    save = store._save_manifest
    monkeypatch.setattr(store, "_save_manifest", lambda value: (_ for _ in ()).throw(
        OSError("atomic replace refused")))
    with pytest.raises(OSError):
        store.publish_capture(capture.capture_id, call_id="a", tool_name="test",
                              source_identity={}, complete=True)
    monkeypatch.setattr(store, "_save_manifest", save)
    assert store.quota() == before and not store.refs()
    assert store.read_capture(capture.capture_id)["content"] == "hello"
