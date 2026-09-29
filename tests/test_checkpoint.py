import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tracefix.checkpoint import CheckpointError, CheckpointStore, ProcessLock


@pytest.fixture
def checkpoint_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix=".checkpoint-test-", dir=Path(__file__).parent) as path:
        yield Path(path)


def test_checkpoint_round_trip_and_identity_verification(checkpoint_dir: Path) -> None:
    identity = {"run_id": "run-1", "workspace": "workspace", "config_sha256": "abc"}
    store = CheckpointStore(checkpoint_dir, identity)

    saved = store.save({"agent": {"step_count": 3}, "messages": []}, sequence=1)

    assert saved.sequence == 1
    assert store.inspect().resumable
    assert store.load().payload == {"agent": {"step_count": 3}, "messages": []}
    assert not store.inspect(current_identity={**identity, "config_sha256": "changed"}).resumable
    with pytest.raises(CheckpointError):
        store.load(current_identity={**identity, "config_sha256": "changed"})


def test_checkpoint_sequence_must_increase(checkpoint_dir: Path) -> None:
    store = CheckpointStore(checkpoint_dir, {"run_id": "run-1"})
    store.save({"state": 1}, sequence=2)

    with pytest.raises(CheckpointError):
        store.save({"state": 2}, sequence=2)


def test_checkpoint_hash_corruption_fails_closed(checkpoint_dir: Path) -> None:
    store = CheckpointStore(checkpoint_dir, {"run_id": "run-1"})
    store.save({"state": 1}, sequence=1)
    envelope = json.loads(store.path.read_text(encoding="utf-8"))
    envelope["payload"]["state"] = 2
    store.path.write_text(json.dumps(envelope), encoding="utf-8")

    inspection = store.inspect()
    assert not inspection.resumable
    assert "payload_hash_mismatch" in inspection.reasons
    with pytest.raises(CheckpointError):
        store.load()


def test_pending_calls_are_visible_and_block_resume(checkpoint_dir: Path) -> None:
    store = CheckpointStore(checkpoint_dir, {"run_id": "run-1"})
    store.save(
        {"state": "before-result"},
        sequence=1,
        pending_calls=[{"call_id": "tool-1", "kind": "apply_patch"}],
    )

    inspection = store.inspect()
    assert not inspection.resumable
    assert inspection.reasons == ("pending_calls",)
    assert inspection.pending_calls == ({"call_id": "tool-1", "kind": "apply_patch"},)


def test_corrupt_checkpoint_inspection_does_not_raise(checkpoint_dir: Path) -> None:
    store = CheckpointStore(checkpoint_dir, {"run_id": "run-1"})
    store.run_dir.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{truncated", encoding="utf-8")

    inspection = store.inspect()

    assert inspection.exists
    assert not inspection.resumable
    assert inspection.reasons == ("checkpoint_corrupt",)


def test_process_lock_is_exclusive_and_released(checkpoint_dir: Path) -> None:
    first = ProcessLock(checkpoint_dir).acquire()
    second = ProcessLock(checkpoint_dir)
    try:
        with pytest.raises(CheckpointError):
            second.acquire()
    finally:
        first.release()

    with ProcessLock(checkpoint_dir):
        pass


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"schema_version": 99}, "unsupported_schema_version"),
        ({"sequence": 0}, "invalid_sequence"),
        ({"sequence": True}, "invalid_sequence"),
        ({"identity": []}, "invalid_identity"),
        ({"payload": []}, "invalid_payload"),
        ({"payload_sha256": "wrong"}, "payload_hash_mismatch"),
        ({"pending_calls": "bad"}, "invalid_pending_calls"),
        ({"pending_calls": [{"call_id": "unknown", "kind": "run_tests"}]}, "pending_calls"),
    ],
)
def test_checkpoint_refuses_incompatible_or_ambiguous_envelopes(
    checkpoint_dir: Path, mutation: dict, reason: str
) -> None:
    store = CheckpointStore(checkpoint_dir, {"run_id": "run-1"})
    store.save({"state": "safe"}, sequence=1)
    envelope = json.loads(store.path.read_text(encoding="utf-8"))
    envelope.update(mutation)
    store.path.write_text(json.dumps(envelope), encoding="utf-8")

    assert reason in store.inspect().reasons
    with pytest.raises(CheckpointError):
        store.load()
    if reason == "pending_calls":
        assert store.save({"state": "reconciled"}, sequence=2).sequence == 2
    else:
        with pytest.raises(CheckpointError):
            store.save({"state": "unsafe"}, sequence=2)


@pytest.mark.parametrize(
    "pending",
    ["bad", ["bad"], [{"call_id": 4}], [{"call_id": "x", "kind": 4}]],
)
def test_checkpoint_rejects_invalid_pending_call_record(checkpoint_dir: Path, pending) -> None:
    store = CheckpointStore(checkpoint_dir, {"run_id": "run-1"})
    with pytest.raises(CheckpointError):
        store.save({"state": "safe"}, sequence=1, pending_calls=pending)
    assert not store.path.exists()


def test_checkpoint_rejects_non_json_and_non_object_payloads(checkpoint_dir: Path) -> None:
    with pytest.raises(CheckpointError):
        CheckpointStore(checkpoint_dir, [])
    store = CheckpointStore(checkpoint_dir, {"run_id": "run-1"})
    with pytest.raises(CheckpointError):
        store.save([], sequence=1)
    with pytest.raises(CheckpointError):
        store.save({"cost": float("nan")}, sequence=1)
    with pytest.raises(CheckpointError):
        store.save({"state": "safe"}, sequence=0)
    assert store.inspect().reasons == ("checkpoint_missing",)
