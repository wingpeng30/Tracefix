"""Atomic, fail-closed storage primitives for local Agent checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tracefix.exceptions import TraceFixError

CHECKPOINT_SCHEMA_VERSION = 1


class CheckpointError(TraceFixError):
    """A checkpoint is corrupt, incompatible, or unsafe to resume."""

    code = "checkpoint_error"


@dataclass(frozen=True)
class CheckpointSnapshot:
    """Validated checkpoint envelope returned by save and load."""

    schema_version: int
    sequence: int
    identity: dict[str, Any]
    payload: dict[str, Any]
    payload_sha256: str
    pending_calls: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class CheckpointInspection:
    """Read-only checkpoint status suitable for a CLI or report."""

    exists: bool
    resumable: bool
    reasons: tuple[str, ...]
    sequence: int | None = None
    payload_sha256: str | None = None
    pending_calls: tuple[dict[str, str], ...] = ()


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CheckpointError("checkpoint data must be finite JSON values") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class CheckpointStore:
    """Store a single versioned checkpoint under a run directory.

    The checkpoint file is replaced atomically. Callers should save only at a
    committed tool-batch boundary; ``pending_calls`` lets inspection enforce
    that boundary without interpreting Agent-specific payloads.
    """

    def __init__(self, run_dir: str | Path, identity: dict[str, Any]) -> None:
        self.run_dir = Path(run_dir).expanduser().resolve()
        self.path = self.run_dir / "checkpoint.json"
        if not isinstance(identity, dict):
            raise CheckpointError("checkpoint identity must be an object")
        # Validate eagerly and detach caller-owned mutable objects.
        self.identity = json.loads(_canonical_json(identity))

    def save(
        self,
        payload: dict[str, Any],
        *,
        sequence: int,
        pending_calls: tuple[dict[str, str], ...] | list[dict[str, str]] = (),
    ) -> CheckpointSnapshot:
        """Atomically persist one snapshot with a strictly increasing sequence."""
        if not isinstance(payload, dict):
            raise CheckpointError("checkpoint payload must be an object")
        if sequence < 1:
            raise CheckpointError("checkpoint sequence must be positive")
        normalized_payload = json.loads(_canonical_json(payload))
        normalized_pending = self._normalize_pending(pending_calls)

        previous = self.inspect(current_identity=self.identity)
        if previous.exists and any(
            reason != "pending_calls" for reason in previous.reasons
        ):
            raise CheckpointError(
                "cannot replace an invalid checkpoint",
                context={"reasons": list(previous.reasons)},
            )
        if previous.exists and previous.sequence is not None and sequence <= previous.sequence:
            raise CheckpointError(
                "checkpoint sequence must increase",
                context={"previous": previous.sequence, "requested": sequence},
            )
        payload_bytes = _canonical_json(normalized_payload)
        envelope = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "sequence": sequence,
            "identity": self.identity,
            "payload": normalized_payload,
            "payload_sha256": _sha256(payload_bytes),
            "pending_calls": normalized_pending,
        }
        serialized = _canonical_json(envelope) + b"\n"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".checkpoint-", suffix=".tmp", dir=self.run_dir
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            self._fsync_directory()
        except OSError as exc:
            raise CheckpointError(
                f"cannot atomically save checkpoint: {exc}",
                context={"path": str(self.path)},
            ) from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return CheckpointSnapshot(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            sequence=sequence,
            identity=self.identity.copy(),
            payload=normalized_payload,
            payload_sha256=_sha256(payload_bytes),
            pending_calls=tuple(normalized_pending),
        )

    def inspect(
        self,
        *,
        current_identity: dict[str, Any] | None = None,
    ) -> CheckpointInspection:
        """Inspect integrity and identity without raising on untrusted disk data."""
        if not self.path.exists():
            return CheckpointInspection(False, False, ("checkpoint_missing",))
        reasons: list[str] = []
        try:
            raw = self.path.read_bytes()
            envelope = json.loads(raw)
            if not isinstance(envelope, dict):
                raise ValueError("root must be an object")
            if envelope.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
                reasons.append("unsupported_schema_version")
            sequence = envelope.get("sequence")
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
                reasons.append("invalid_sequence")
                sequence = None
            identity = envelope.get("identity")
            payload = envelope.get("payload")
            if not isinstance(identity, dict):
                reasons.append("invalid_identity")
            if not isinstance(payload, dict):
                reasons.append("invalid_payload")
            digest = envelope.get("payload_sha256")
            if isinstance(payload, dict):
                actual_digest = _sha256(_canonical_json(payload))
                if not isinstance(digest, str) or digest != actual_digest:
                    reasons.append("payload_hash_mismatch")
            else:
                actual_digest = None
            pending_raw = envelope.get("pending_calls", [])
            try:
                pending = self._normalize_pending(pending_raw)
            except CheckpointError:
                pending = []
                reasons.append("invalid_pending_calls")
            if pending:
                reasons.append("pending_calls")
            expected_identity = self.identity if current_identity is None else current_identity
            if isinstance(identity, dict):
                try:
                    if _canonical_json(identity) != _canonical_json(expected_identity):
                        reasons.append("identity_mismatch")
                except CheckpointError:
                    reasons.append("invalid_current_identity")
            valid = not reasons
            return CheckpointInspection(
                exists=True,
                resumable=valid,
                reasons=tuple(dict.fromkeys(reasons)),
                sequence=sequence,
                payload_sha256=actual_digest,
                pending_calls=tuple(pending),
            )
        except (
            CheckpointError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
        ):
            return CheckpointInspection(
                exists=True,
                resumable=False,
                reasons=("checkpoint_corrupt",),
            )

    def load(
        self,
        *,
        current_identity: dict[str, Any] | None = None,
    ) -> CheckpointSnapshot:
        """Return a verified snapshot, refusing every ambiguous state."""
        inspection = self.inspect(current_identity=current_identity)
        if not inspection.resumable:
            raise CheckpointError(
                "checkpoint is not safe to resume",
                context={"reasons": list(inspection.reasons)},
            )
        try:
            envelope = json.loads(self.path.read_bytes())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CheckpointError("checkpoint could not be read") from exc
        return CheckpointSnapshot(
            schema_version=envelope["schema_version"],
            sequence=envelope["sequence"],
            identity=envelope["identity"],
            payload=envelope["payload"],
            payload_sha256=envelope["payload_sha256"],
            pending_calls=tuple(envelope["pending_calls"]),
        )

    @staticmethod
    def _normalize_pending(
        values: tuple[dict[str, str], ...] | list[dict[str, str]],
    ) -> list[dict[str, str]]:
        if not isinstance(values, (tuple, list)):
            raise CheckpointError("pending_calls must be a list")
        normalized: list[dict[str, str]] = []
        for item in values:
            if not isinstance(item, dict) or not isinstance(item.get("call_id"), str):
                raise CheckpointError("pending call requires a string call_id")
            entry = {"call_id": item["call_id"]}
            kind = item.get("kind")
            if kind is not None:
                if not isinstance(kind, str):
                    raise CheckpointError("pending call kind must be a string")
                entry["kind"] = kind
            normalized.append(entry)
        return normalized

    def _fsync_directory(self) -> None:
        """Persist the directory entry where supported by the host OS."""
        if os.name == "nt":
            return
        descriptor = os.open(self.run_dir, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class ProcessLock:
    """Exclusive per-run lock released by the OS when its process exits."""

    def __init__(self, run_dir: str | Path) -> None:
        self.run_dir = Path(run_dir).expanduser().resolve()
        self.path = self.run_dir / ".tracefix.lock"
        self._stream: Any = None

    def acquire(self) -> ProcessLock:
        if self._stream is not None:
            raise CheckpointError("process lock is already held by this instance")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                # Windows byte-range locking requires a byte to exist in the file.
                if self.path.stat().st_size == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            stream.close()
            raise CheckpointError(
                "another process holds the run lock",
                context={"path": str(self.path)},
            ) from exc
        self._stream = stream
        return self

    def release(self) -> None:
        if self._stream is None:
            return
        stream, self._stream = self._stream, None
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self) -> ProcessLock:
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()
