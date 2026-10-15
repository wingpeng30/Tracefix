"""Bounded, run-scoped tool artifacts with immutable redacted read views.

The store is deliberately independent of the agent, model gateway, memory and
Docker implementations.  A run owns one store; callers pass opaque artifact
IDs and never receive a host path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

ARTIFACT_SCHEMA_VERSION = 1
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_RUN_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_ARCHIVE_BYTES = MAX_RUN_BYTES + 4 * 1024 * 1024
MAX_RUN_ARTIFACTS = 128
MAX_PAGE_CHARS = 4096
MAX_SESSION_QUERIES = 64
MAX_SESSION_RETURNED_CHARS = 128 * 1024
MAX_SEARCH_MATCHES = 20
MAX_SEARCH_QUERY_CHARS = 256
_REDACTION_VERSION = "tracefix-redaction-v1"
_REDACTION_PATTERN = re.compile(
    r"(?im)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]{8,}"
    r"|(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"
    r"|(-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----).*?"
    r"(-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
    r"|(AKIA[0-9A-Z]{16})"
    r"|((?:api[_-]?key|access[_-]?token|secret|password)\s*[=:]\s*)"
    r"([^\s,;\"']{8,})",
    re.IGNORECASE | re.DOTALL,
)


class ArtifactError(ValueError):
    """An artifact is invalid, unavailable, over quota or failed integrity."""


class ArtifactRef(BaseModel):
    """Path-free public identity and integrity receipt for one captured call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    run_id: str = Field(min_length=1, max_length=256)
    call_id: str = Field(min_length=1, max_length=256)
    tool_name: str = Field(min_length=1, max_length=128)
    source_identity: dict[str, str] = Field(default_factory=dict)
    raw_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    view_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    raw_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    view_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    complete: bool
    integrity: Literal["verified", "incomplete"]
    redaction_policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_source_identity(self) -> ArtifactRef:
        if len(self.source_identity) > 16 or any(
            not key or len(key) > 64 or len(value) > 1024 or "\x00" in key or "\x00" in value
            for key, value in self.source_identity.items()
        ):
            raise ValueError("artifact source identity exceeds its bounded contract")
        if (self.complete and self.integrity != "verified") or (
            not self.complete and self.integrity != "incomplete"
        ):
            raise ValueError("artifact completeness and integrity status disagree")
        return self


class ArtifactPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact: ArtifactRef
    stream: Literal["stdout", "stderr"]
    content: str
    offset: int = Field(ge=0)
    next_offset: int = Field(ge=0)
    eof: bool


class ArtifactSearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact: ArtifactRef
    stream: Literal["stdout", "stderr"]
    offset: int = Field(ge=0)
    excerpt: str


class ArtifactSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str
    hits: tuple[ArtifactSearchHit, ...]
    truncated: bool
    queries_remaining: int = Field(ge=0)
    characters_remaining: int = Field(ge=0)


class ArtifactQuota(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifacts: int = Field(ge=0, le=MAX_RUN_ARTIFACTS)
    storage_bytes: int = Field(ge=0, le=MAX_RUN_BYTES)
    queries: int = Field(ge=0, le=MAX_SESSION_QUERIES)
    returned_chars: int = Field(ge=0, le=MAX_SESSION_RETURNED_CHARS)


class CaptureReservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capture_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    raw_budget: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_linklike(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _combined_sha(parts: tuple[tuple[str, bytes], ...]) -> str:
    digest = hashlib.sha256()
    for name, content in parts:
        digest.update(name.encode("ascii") + b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _combined_file_sha(parts: tuple[tuple[str, Path, int], ...]) -> str:
    """Hash named files with the same framing as _combined_sha, without buffering them."""
    digest = hashlib.sha256()
    for name, path, size in parts:
        digest.update(name.encode("ascii") + b"\0")
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _original_character_offset(text: str, folded_offset: int) -> int:
    """Map a casefolded-string position without allocating a per-character index table."""
    current = 0
    for index, char in enumerate(text):
        following = current + len(char.casefold())
        if following > folded_offset:
            return index
        current = following
    return len(text)


def _redact(text: str, secret_values: tuple[str, ...]) -> str:
    def fixed(match: re.Match[str]) -> str:
        if match.group(1):
            return match.group(1) + "[REDACTED]"
        if match.group(2):
            return match.group(2) + "[REDACTED]"
        if match.group(3):
            return match.group(3) + "[REDACTED]" + (match.group(4) or "")
        if match.group(5):
            return "[REDACTED]"
        return (match.group(6) or "") + "[REDACTED]"

    text = _REDACTION_PATTERN.sub(fixed, text)
    for value in secret_values:
        if value:
            text = text.replace(value, "[REDACTED]")
    return text


def _bounded_text_bytes(text: str, maximum: int) -> bytes:
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum:
        return encoded
    clipped = encoded[:maximum].decode("utf-8", errors="ignore")
    return clipped.encode("utf-8")


class ArtifactStore:
    """Atomic per-run artifact store; read quotas persist with the manifest."""

    def __init__(
        self,
        root: str | Path,
        *,
        run_id: str,
        workspace: str | Path | None = None,
        secret_values: tuple[str, ...] = (),
        streaming_enabled: bool = False,
    ) -> None:
        if not run_id or len(run_id) > 256:
            raise ArtifactError("invalid artifact run identity")
        self.root = Path(root).expanduser().absolute()
        self.run_id = run_id
        self.workspace = Path(workspace).expanduser().resolve() if workspace is not None else None
        if self.workspace is not None:
            resolved = self.root.resolve()
            if resolved == self.workspace or self.workspace in resolved.parents:
                raise ArtifactError("artifact storage must be outside the source workspace")
        self.secret_values = tuple(sorted(set(secret_values), key=lambda item: (-len(item), item)))
        self.streaming_enabled = streaming_enabled
        self._capture_writers: dict[str, dict[str, object]] = {}
        self._failed_captures: set[str] = set()
        self.redaction_policy_sha256 = _sha(
            _canonical(
                {
                    "version": (_REDACTION_VERSION + "+stream-v1"
                                if streaming_enabled else _REDACTION_VERSION),
                    "configured_values": "redact-non-empty-values-v1",
                }
            )
        )
        self._lock = threading.RLock()
        self.objects = self.root / "objects"
        self.manifest_path = self.root / "manifest.json"
        self._initialize()

    def _initialize(self) -> None:
        if _is_linklike(self.root):
            raise ArtifactError("artifact root cannot be a symbolic link")
        self.root.mkdir(parents=True, exist_ok=True)
        if _is_linklike(self.objects):
            raise ArtifactError("artifact object directory cannot be a symbolic link")
        if _is_linklike(self.manifest_path):
            raise ArtifactError("artifact manifest cannot be a symbolic link")
        self.objects.mkdir(exist_ok=True)
        if self.manifest_path.exists():
            manifest = self._load_manifest()
            if manifest["run_id"] != self.run_id:
                raise ArtifactError("artifact store belongs to another run")
            if manifest["redaction_policy_sha256"] != self.redaction_policy_sha256:
                raise ArtifactError("artifact redaction policy changed")
            self._validate_inventory(manifest)
        else:
            if any(self.objects.iterdir()):
                raise ArtifactError("artifact object inventory exists without a manifest")
            manifest = self._empty_manifest()
            self._save_manifest(manifest)

    def _empty_manifest(self) -> dict:
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "redaction_policy_sha256": self.redaction_policy_sha256,
            "query_count": 0,
            "returned_chars": 0,
            "artifacts": {},
        }

    def _load_manifest(self) -> dict:
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactError("artifact manifest is missing or corrupt") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != ARTIFACT_SCHEMA_VERSION
            or not isinstance(value.get("artifacts"), dict)
            or type(value.get("query_count")) is not int
            or type(value.get("returned_chars")) is not int
            or value["query_count"] < 0
            or value["returned_chars"] < 0
            or not isinstance(value.get("run_id"), str)
            or not isinstance(value.get("redaction_policy_sha256"), str)
        ):
            raise ArtifactError("invalid artifact manifest")
        captures = value.get("captures", {})
        if not isinstance(captures, dict) or len(captures) > MAX_RUN_ARTIFACTS:
            raise ArtifactError("invalid live capture inventory")
        if captures and not self.streaming_enabled:
            raise ArtifactError("live captures require streaming capability")
        for capture_id, entry in captures.items():
            if (not isinstance(capture_id, str)
                    or re.fullmatch(r"[a-f0-9]{32}", capture_id) is None
                    or not isinstance(entry, dict)
                    or set(entry) != {"budget", "files", "complete"}
                    or type(entry["budget"]) is not int
                    or not 0 <= entry["budget"] <= MAX_ARTIFACT_BYTES
                    or type(entry["complete"]) is not bool
                    or not isinstance(entry["files"], dict)
                    or set(entry["files"]) != {
                        "raw_stdout", "raw_stderr", "view_stdout", "view_stderr"}):
                raise ArtifactError("invalid live capture reservation")
            for receipt in entry["files"].values():
                if (not isinstance(receipt, list) or len(receipt) != 2
                        or type(receipt[0]) is not int or not 0 <= receipt[0] <= entry["budget"]
                        or not isinstance(receipt[1], str)
                        or re.fullmatch(r"[a-f0-9]{64}", receipt[1]) is None):
                    raise ArtifactError("invalid live capture receipt")
            for kind in ("raw", "view"):
                if sum(entry["files"][f"{kind}_{stream}"][0]
                       for stream in ("stdout", "stderr")) > entry["budget"]:
                    raise ArtifactError("live capture exceeds its reservation")
        try:
            storage_used = self._storage_used(value)
        except (KeyError, TypeError, IndexError, AttributeError) as exc:
            raise ArtifactError("invalid artifact storage inventory") from exc
        if (storage_used > MAX_RUN_BYTES
                or len(value["artifacts"]) + len(captures) > MAX_RUN_ARTIFACTS):
            raise ArtifactError("artifact reservations exceed run quota")
        return value

    @staticmethod
    def _storage_used(manifest: dict) -> int:
        return sum(info[2] for entry in manifest["artifacts"].values()
                   for info in entry["files"].values()) + sum(
                       entry["budget"] * 2 for entry in manifest.get("captures", {}).values())

    def _redact_view(self, text: str) -> str:
        if not self.streaming_enabled:
            return _redact(text, self.secret_values)
        from tracefix.jobs.redaction import StreamingRedactor
        redactor = StreamingRedactor(secret_values=self.secret_values)
        return redactor.feed(text.encode("utf-8")) + redactor.finish()

    def _save_manifest(self, value: dict) -> None:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.manifest_path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @staticmethod
    def _entry_ref(entry: dict) -> ArtifactRef:
        try:
            return ArtifactRef.model_validate(entry["ref"])
        except Exception as exc:
            raise ArtifactError("artifact reference in manifest is invalid") from exc

    def _validate_inventory(self, manifest: dict) -> None:
        if (
            manifest.get("run_id") != self.run_id
            or manifest.get("redaction_policy_sha256") != self.redaction_policy_sha256
        ):
            raise ArtifactError("artifact store identity or redaction policy changed")
        if len(manifest["artifacts"]) > MAX_RUN_ARTIFACTS:
            raise ArtifactError("artifact manifest exceeds its count limit")
        storage = 0
        for artifact_id, entry in manifest["artifacts"].items():
            ref = self._entry_ref(entry)
            if artifact_id != ref.artifact_id or ref.run_id != self.run_id:
                raise ArtifactError("artifact identity does not match its run")
            if set(entry.get("files", {})) != {
                "raw_stdout",
                "raw_stderr",
                "view_stdout",
                "view_stderr",
            }:
                raise ArtifactError("artifact file inventory is incomplete")
            expected_paths = {
                f"raw_{name}": f"objects/{artifact_id}/raw-{name}.bin"
                for name in ("stdout", "stderr")
            } | {
                f"view_{name}": f"objects/{artifact_id}/view-{name}.txt"
                for name in ("stdout", "stderr")
            }
            raw_parts: list[tuple[str, Path, int]] = []
            view_parts: list[tuple[str, Path, int]] = []
            for field, receipt in entry["files"].items():
                if not isinstance(receipt, list | tuple) or len(receipt) != 3:
                    raise ArtifactError("artifact file receipt is invalid")
                rel, expected, size = receipt
                if (
                    rel != expected_paths[field]
                    or not isinstance(expected, str)
                    or re.fullmatch(r"[a-f0-9]{64}", expected) is None
                    or type(size) is not int
                    or not 0 <= size <= MAX_ARTIFACT_BYTES
                ):
                    raise ArtifactError("artifact file receipt is invalid")
                path = self._owned_file(rel)
                if path.stat().st_size != size or _file_sha(path) != expected:
                    raise ArtifactError("artifact content failed integrity validation")
                storage += size
                stream_name = field.rsplit("_", 1)[1]
                if field.startswith("raw_"):
                    raw_parts.append((stream_name, path, size))
                else:
                    view_parts.append((stream_name, path, size))
            raw_bytes = sum(entry["files"][f"raw_{name}"][2] for name in ("stdout", "stderr"))
            if raw_bytes != ref.raw_bytes:
                raise ArtifactError("artifact raw byte count does not match its receipt")
            view_bytes = sum(entry["files"][f"view_{name}"][2] for name in ("stdout", "stderr"))
            if view_bytes != ref.view_bytes:
                raise ArtifactError("artifact view byte count does not match its receipt")
            if (
                _combined_file_sha(
                    tuple(sorted(raw_parts, key=lambda part: ("stdout", "stderr").index(part[0])))
                )
                != ref.raw_sha256
                or _combined_file_sha(
                    tuple(sorted(view_parts, key=lambda part: ("stdout", "stderr").index(part[0])))
                )
                != ref.view_sha256
            ):
                raise ArtifactError("artifact aggregate content failed integrity validation")
            if ref.redaction_policy_sha256 != self.redaction_policy_sha256:
                raise ArtifactError("artifact redaction identity changed")
        if storage > MAX_RUN_BYTES:
            raise ArtifactError("artifact manifest exceeds its storage limit")
        if manifest["query_count"] > MAX_SESSION_QUERIES:
            raise ArtifactError("artifact query quota is invalid")
        if manifest["returned_chars"] > MAX_SESSION_RETURNED_CHARS:
            raise ArtifactError("artifact read quota is invalid")
        actual = {path.name for path in self.objects.iterdir()}
        expected = set(manifest["artifacts"])
        if actual != expected:
            raise ArtifactError("artifact object inventory differs from its manifest")
        captures = self.root / "captures"
        if _is_linklike(captures) or (captures.exists() and not captures.is_dir()):
            raise ArtifactError("live capture directory is unsafe")
        actual_captures = {path.name for path in captures.iterdir()} if captures.exists() else set()
        if actual_captures != set(manifest.get("captures", {})):
            raise ArtifactError("live capture inventory differs from its manifest")
        for capture_id, entry in manifest.get("captures", {}).items():
            for field in entry["files"]:
                self._capture_bytes(capture_id, entry, field)

    def _owned_file(self, relative: str) -> Path:
        path_text = PurePosixPath(relative)
        if (
            path_text.is_absolute()
            or not path_text.parts
            or any(part in {".", ".."} for part in path_text.parts)
            or "\\" in relative
        ):
            raise ArtifactError("artifact path is unsafe")
        path = self.root.joinpath(*path_text.parts)
        current = self.root
        for part in path_text.parts:
            current = current / part
            if _is_linklike(current):
                raise ArtifactError("artifact path contains a symbolic link")
        if not path.is_file() or path.stat().st_nlink > 1:
            raise ArtifactError("artifact file is missing")
        return path

    def capture_budget(self) -> int:
        """Return the maximum combined raw bytes a process may persist."""
        with self._lock:
            manifest = self._load_manifest()
            if len(manifest["artifacts"]) + len(manifest.get("captures", {})) >= MAX_RUN_ARTIFACTS:
                return 0
            used = self._storage_used(manifest)
            return max(0, min(MAX_ARTIFACT_BYTES, (MAX_RUN_BYTES - used) // 2))

    def reserve_capture(self) -> CaptureReservation:
        """Persist one count/byte reservation before accepting any live output."""
        if not self.streaming_enabled:
            raise ArtifactError("streaming capability is disabled")
        with self._lock:
            manifest = self._load_manifest()
            if len(manifest["artifacts"]) + len(manifest.get("captures", {})) >= MAX_RUN_ARTIFACTS:
                raise ArtifactError("artifact count quota is exhausted")
            reservation = CaptureReservation(capture_id=uuid4().hex,
                                             raw_budget=self.capture_budget())
            parent = self.root / "captures"
            if _is_linklike(parent):
                raise ArtifactError("capture directory cannot be a symbolic link")
            parent.mkdir(exist_ok=True)
            directory = parent / reservation.capture_id
            directory.mkdir()
            receipts = {}
            writers = {}
            try:
                for kind in ("raw", "view"):
                    for stream in ("stdout", "stderr"):
                        field = f"{kind}_{stream}"
                        suffix = "bin" if kind == "raw" else "txt"
                        (directory / f"{kind}-{stream}.{suffix}").write_bytes(b"")
                        receipts[field] = [0, _sha(b"")]
                        writers[field] = hashlib.sha256()
                manifest.setdefault("captures", {})[reservation.capture_id] = {
                    "budget": reservation.raw_budget, "files": receipts, "complete": True,
                }
                self._save_manifest(manifest)
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise
            self._capture_writers[reservation.capture_id] = writers
            return reservation

    @staticmethod
    def _capture_entry(manifest: dict, capture_id: str) -> dict:
        if not isinstance(capture_id, str) or re.fullmatch(r"[a-f0-9]{32}", capture_id) is None:
            raise ArtifactError("invalid opaque live capture ID")
        try:
            return manifest["captures"][capture_id]
        except KeyError as exc:
            raise ArtifactError("live capture is not available in this run") from exc

    def _capture_path(self, capture_id: str, field: str) -> Path:
        kind, stream = field.split("_")
        suffix = "bin" if kind == "raw" else "txt"
        return self._owned_file(f"captures/{capture_id}/{kind}-{stream}.{suffix}")

    def _capture_bytes(self, capture_id: str, entry: dict, field: str) -> bytes:
        path = self._capture_path(capture_id, field)
        size, digest = entry["files"][field]
        if not size <= path.stat().st_size <= entry["budget"]:
            raise ArtifactError("live capture integrity size mismatch")
        with path.open("rb") as stream:
            content = stream.read(size)
        if len(content) != size or _sha(content) != digest:
            raise ArtifactError("live capture integrity hash mismatch")
        return content

    def append_capture(self, capture_id: str, *, stream: str, raw: bytes, view: str) -> bool:
        """Commit raw and safe-view prefixes together; return False when either is clipped."""
        with self._lock:
            if capture_id in self._failed_captures:
                raise ArtifactError("live capture writer has failed; only sealing is permitted")
            try:
                return self._append_capture(capture_id, stream=stream, raw=raw, view=view)
            except BaseException:
                self._failed_captures.add(capture_id)
                raise

    def _append_capture(self, capture_id: str, *, stream: str, raw: bytes, view: str) -> bool:
        if stream not in {"stdout", "stderr"}:
            raise ArtifactError("unknown capture stream")
        with self._lock:
            manifest = self._load_manifest()
            entry = self._capture_entry(manifest, capture_id)
            if capture_id not in self._capture_writers:
                raise ArtifactError("live capture writer cannot resume after process restart")
            updated = json.loads(json.dumps(manifest))
            target = updated["captures"][capture_id]
            digests = dict(self._capture_writers[capture_id])
            accepted_all = True
            for kind, data in (("raw", raw), ("view", view.encode("utf-8"))):
                field = f"{kind}_{stream}"
                self._capture_bytes(capture_id, entry, field)
                used = sum(entry["files"][f"{kind}_{name}"][0]
                           for name in ("stdout", "stderr"))
                accepted = data[:max(0, entry["budget"] - used)]
                if kind == "view":
                    accepted = accepted.decode("utf-8", "ignore").encode("utf-8")
                accepted_all &= len(accepted) == len(data)
                path = self._capture_path(capture_id, field)
                committed = entry["files"][field][0]
                with path.open("r+b") as output:
                    output.truncate(committed)
                    output.seek(committed)
                    if output.write(accepted) != len(accepted):
                        raise ArtifactError("live capture storage write was incomplete")
                    output.flush()
                    os.fsync(output.fileno())
                digest = digests[field].copy()
                digest.update(accepted)
                digests[field] = digest
                target["files"][field] = [committed + len(accepted), digest.hexdigest()]
            target["complete"] &= accepted_all
            self._save_manifest(updated)
            self._capture_writers[capture_id] = digests
            return accepted_all

    def read_capture(self, capture_id: str, *, stream: str = "stdout", offset: int = 0,
                     limit: int = MAX_PAGE_CHARS) -> dict[str, object]:
        if (stream not in {"stdout", "stderr"} or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= MAX_PAGE_CHARS):
            raise ArtifactError("live capture page bounds are invalid")
        with self._lock:
            manifest = self._load_manifest()
            entry = self._capture_entry(manifest, capture_id)
            text = self._capture_bytes(capture_id, entry, f"view_{stream}").decode("utf-8")
            if offset > len(text):
                raise ArtifactError("live capture offset is beyond committed output")
            page = text[offset:offset + limit]
            self._consume_query(manifest, len(page))
            return {"capture_id": capture_id, "stream": stream, "offset": offset,
                    "next_offset": offset + len(page), "content": page,
                    "at_committed_end": offset + len(page) == len(text),
                    "final": False, "complete": entry["complete"]}

    def publish_capture(self, capture_id: str, *, call_id: str, tool_name: str,
                        source_identity: dict[str, str], complete: bool) -> ArtifactRef:
        """Move committed files to immutable storage and settle the reservation atomically."""
        with self._lock:
            manifest = self._load_manifest()
            entry = self._capture_entry(manifest, capture_id)
            # A restarted process may inspect unknown output, but cannot pretend it completed.
            if capture_id not in self._capture_writers:
                raise ArtifactError("live capture writer cannot publish after process restart")
            for field in entry["files"]:
                self._capture_bytes(capture_id, entry, field)
                with self._capture_path(capture_id, field).open("r+b") as output:
                    output.truncate(entry["files"][field][0])
                    output.flush()
                    os.fsync(output.fileno())
            raw_parts = tuple((name, self._capture_path(capture_id, f"raw_{name}"),
                               entry["files"][f"raw_{name}"][0]) for name in ("stdout", "stderr"))
            view_parts = tuple((name, self._capture_path(capture_id, f"view_{name}"),
                                entry["files"][f"view_{name}"][0]) for name in ("stdout", "stderr"))
            artifact_id = uuid4().hex
            complete = complete and entry["complete"] and capture_id not in self._failed_captures
            ref = ArtifactRef(
                artifact_id=artifact_id, run_id=self.run_id, call_id=call_id, tool_name=tool_name,
                source_identity=source_identity, raw_sha256=_combined_file_sha(raw_parts),
                view_sha256=_combined_file_sha(view_parts), raw_bytes=sum(x[2] for x in raw_parts),
                view_bytes=sum(x[2] for x in view_parts), complete=complete,
                integrity="verified" if complete else "incomplete",
                redaction_policy_sha256=self.redaction_policy_sha256,
            )
            files = {}
            for field, (size, digest) in entry["files"].items():
                name = self._capture_path(capture_id, field).name
                files[field] = [f"objects/{artifact_id}/{name}", digest, size]
            origin, destination = self.root / "captures" / capture_id, self.objects / artifact_id
            os.replace(origin, destination)
            try:
                del manifest["captures"][capture_id]
                manifest["artifacts"][artifact_id] = {
                    "ref": ref.model_dump(mode="json"), "files": files,
                }
                self._save_manifest(manifest)
            except BaseException:
                os.replace(destination, origin)
                raise
            del self._capture_writers[capture_id]
            self._failed_captures.discard(capture_id)
            return ref

    def publish_process(
        self,
        *,
        call_id: str,
        tool_name: str,
        source_identity: dict[str, str],
        stdout: bytes,
        stderr: bytes,
        complete: bool,
    ) -> ArtifactRef:
        """Publish captured raw streams and their bounded redacted UTF-8 views."""
        if len(stdout) + len(stderr) > MAX_ARTIFACT_BYTES:
            raise ArtifactError("captured raw streams exceed the artifact limit")
        with self._lock:
            manifest = self._load_manifest()
            if manifest["run_id"] != self.run_id:
                raise ArtifactError("artifact store run identity changed")
            if len(manifest["artifacts"]) + len(manifest.get("captures", {})) >= MAX_RUN_ARTIFACTS:
                raise ArtifactError("artifact count quota is exhausted")
            used = self._storage_used(manifest)
            available = MAX_RUN_BYTES - used
            raw_total = len(stdout) + len(stderr)
            if raw_total > available:
                raise ArtifactError("artifact storage quota is exhausted")
            raw_parts = (("stdout", stdout), ("stderr", stderr))
            view_parts_list: list[tuple[str, bytes]] = []
            view_budget = min(MAX_ARTIFACT_BYTES, available - raw_total)
            view_used = 0
            for name, content in raw_parts:
                decoded = content.decode("utf-8", errors="replace")
                redacted = self._redact_view(decoded)
                view = _bounded_text_bytes(redacted, max(0, view_budget - view_used))
                view_used += len(view)
                view_parts_list.append((name, view))
            views = tuple(view_parts_list)
            if view_used < sum(
                len(self._redact_view(data.decode("utf-8", "replace")).encode("utf-8"))
                for _, data in raw_parts
            ):
                complete = False
            raw_sha = _combined_sha(raw_parts)
            view_sha = _combined_sha(views)
            artifact_id = uuid4().hex
            created_at = datetime.now(UTC)
            ref = ArtifactRef(
                artifact_id=artifact_id,
                run_id=self.run_id,
                call_id=call_id,
                tool_name=tool_name,
                source_identity=dict(source_identity),
                raw_sha256=raw_sha,
                view_sha256=view_sha,
                raw_bytes=raw_total,
                view_bytes=view_used,
                complete=complete,
                integrity="verified" if complete else "incomplete",
                redaction_policy_sha256=self.redaction_policy_sha256,
                created_at=created_at,
            )
            temp = self.objects / f".{artifact_id}.partial"
            destination = self.objects / artifact_id
            temp.mkdir()
            files: dict[str, tuple[str, str, int]] = {}
            try:
                for stream_name, content in raw_parts:
                    for kind, data in (("raw", content), ("view", dict(views)[stream_name])):
                        suffix = "bin" if kind == "raw" else "txt"
                        filename = f"{kind}-{stream_name}.{suffix}"
                        (temp / filename).write_bytes(data)
                        files[f"{kind}_{stream_name}"] = (
                            f"objects/{artifact_id}/{filename}",
                            _sha(data),
                            len(data),
                        )
                os.replace(temp, destination)
                updated = dict(manifest)
                updated["artifacts"] = dict(manifest["artifacts"])
                updated["artifacts"][artifact_id] = {
                    "ref": ref.model_dump(mode="json"),
                    "files": {name: list(value) for name, value in files.items()},
                }
                self._save_manifest(updated)
            except BaseException:
                shutil.rmtree(temp, ignore_errors=True)
                if destination.exists() and artifact_id not in manifest["artifacts"]:
                    shutil.rmtree(destination, ignore_errors=True)
                raise
            return ref

    def refs(self) -> tuple[ArtifactRef, ...]:
        with self._lock:
            manifest = self._load_manifest()
            return tuple(
                self._entry_ref(entry) for _, entry in sorted(manifest["artifacts"].items())
            )

    def quota(self) -> ArtifactQuota:
        with self._lock:
            manifest = self._load_manifest()
            used = self._storage_used(manifest)
            return ArtifactQuota(
                artifacts=len(manifest["artifacts"]) + len(manifest.get("captures", {})),
                storage_bytes=used,
                queries=manifest["query_count"],
                returned_chars=manifest["returned_chars"],
            )

    def checkpoint_identity(self) -> dict[str, object]:
        """Bind a local checkpoint to the full immutable manifest and cumulative quota."""
        with self._lock:
            manifest = self._load_manifest()
            if manifest.get("captures"):
                raise ArtifactError("active captures cannot enter a safe checkpoint")
            self._validate_inventory(manifest)
            raw = self.manifest_path.read_bytes()
            quota = self.quota()
            return {"manifest_sha256": _sha(raw), "quota": quota.model_dump(mode="json")}

    def verify_checkpoint_identity(self, saved: object) -> None:
        if not isinstance(saved, dict) or saved != self.checkpoint_identity():
            raise ArtifactError("artifact checkpoint identity or cumulative quota changed")

    def read(
        self,
        artifact_id: str,
        *,
        stream: Literal["stdout", "stderr"] = "stdout",
        offset: int = 0,
        limit: int = MAX_PAGE_CHARS,
    ) -> ArtifactPage:
        if not isinstance(artifact_id, str) or re.fullmatch(r"[a-f0-9]{32}", artifact_id) is None:
            raise ArtifactError("invalid opaque artifact ID")
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= MAX_PAGE_CHARS
        ):
            raise ArtifactError("artifact page bounds are invalid")
        if stream not in {"stdout", "stderr"}:
            raise ArtifactError("unknown artifact stream")
        with self._lock:
            manifest = self._load_manifest()
            try:
                entry = manifest["artifacts"][artifact_id]
            except KeyError as exc:
                raise ArtifactError("artifact is not available in this run") from exc
            ref = self._entry_ref(entry)
            view_entry = entry["files"][f"view_{stream}"]
            if view_entry[0] != f"objects/{artifact_id}/view-{stream}.txt":
                raise ArtifactError("artifact retrieval path differs from its receipt")
            path = self._owned_file(view_entry[0])
            raw = path.read_bytes()
            if len(raw) != view_entry[2] or _sha(raw) != view_entry[1]:
                raise ArtifactError("artifact retrieval view failed integrity validation")
            content = raw.decode("utf-8", errors="strict")
            if offset > len(content):
                raise ArtifactError("artifact offset is beyond end of stream")
            page = content[offset : offset + limit]
            self._consume_query(manifest, len(page))
            next_offset = offset + len(page)
            return ArtifactPage(
                artifact=ref,
                stream=stream,
                content=page,
                offset=offset,
                next_offset=next_offset,
                eof=next_offset >= len(content),
            )

    def search(
        self,
        query: str,
        *,
        max_matches: int = MAX_SEARCH_MATCHES,
        excerpt_chars: int = 160,
    ) -> ArtifactSearchResult:
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_SEARCH_QUERY_CHARS:
            raise ArtifactError(
                "artifact search query must be a non-empty literal of at most 256 chars"
            )
        if type(max_matches) is not int or not 1 <= max_matches <= MAX_SEARCH_MATCHES:
            raise ArtifactError("artifact search limit is outside 1..20")
        if type(excerpt_chars) is not int or not 32 <= excerpt_chars <= 512:
            raise ArtifactError("artifact search excerpt limit is invalid")
        needle = query.casefold()
        if not needle:
            raise ArtifactError("artifact search query normalizes to an empty literal")
        with self._lock:
            manifest = self._load_manifest()
            hits: list[ArtifactSearchHit] = []
            returned = 0
            truncated = False
            for artifact_id, entry in sorted(manifest["artifacts"].items()):
                ref = self._entry_ref(entry)
                for stream in ("stdout", "stderr"):
                    record = entry["files"][f"view_{stream}"]
                    if record[0] != f"objects/{artifact_id}/view-{stream}.txt":
                        raise ArtifactError("artifact retrieval path differs from its receipt")
                    path = self._owned_file(record[0])
                    raw = path.read_bytes()
                    if len(raw) != record[2] or _sha(raw) != record[1]:
                        raise ArtifactError("artifact retrieval view failed integrity validation")
                    content = raw.decode("utf-8", errors="strict")
                    folded = content.casefold()
                    cursor = 0
                    while True:
                        at = folded.find(needle, cursor)
                        if at < 0:
                            break
                        original_at = _original_character_offset(content, at)
                        left = max(0, original_at - excerpt_chars // 2)
                        right = min(len(content), left + excerpt_chars)
                        excerpt = content[left:right]
                        if len(hits) >= max_matches or returned + len(excerpt) > (
                            MAX_SESSION_RETURNED_CHARS - manifest["returned_chars"]
                        ):
                            truncated = True
                            break
                        hits.append(
                            ArtifactSearchHit(
                                artifact=ref, stream=stream, offset=left, excerpt=excerpt
                            )
                        )
                        returned += len(excerpt)
                        cursor = at + max(1, len(needle))
                    if truncated:
                        break
                if truncated:
                    break
            manifest = self._consume_query(manifest, returned)
            return ArtifactSearchResult(
                query=query,
                hits=tuple(hits),
                truncated=truncated,
                queries_remaining=MAX_SESSION_QUERIES - manifest["query_count"],
                characters_remaining=MAX_SESSION_RETURNED_CHARS - manifest["returned_chars"],
            )

    def _consume_query(self, manifest: dict, returned_chars: int) -> dict:
        if manifest["query_count"] >= MAX_SESSION_QUERIES:
            raise ArtifactError("artifact query quota is exhausted")
        if manifest["returned_chars"] + returned_chars > MAX_SESSION_RETURNED_CHARS:
            raise ArtifactError("artifact returned-character quota is exhausted")
        updated = dict(manifest)
        updated["query_count"] += 1
        updated["returned_chars"] += returned_chars
        self._save_manifest(updated)
        return updated

    def export_archive(self, stream: BinaryIO) -> dict:
        """Stream a self-describing archive; no workspace or host path is included."""
        with self._lock:
            manifest = self._load_manifest()
            if manifest.get("captures"):
                raise ArtifactError("active captures cannot be exported as final artifacts")
            self._validate_inventory(manifest)
            serialized = _canonical(manifest)
            if len(serialized) > 2 * 1024 * 1024:
                raise ArtifactError("artifact manifest exceeds transfer limit")
            with tarfile.open(fileobj=stream, mode="w|") as archive:
                _tar_bytes(archive, "manifest.json", serialized)
                for _, entry in sorted(manifest["artifacts"].items()):
                    for _, (relative, expected_sha, size) in sorted(entry["files"].items()):
                        path = self._owned_file(relative)
                        if path.stat().st_size != size or _file_sha(path) != expected_sha:
                            raise ArtifactError("artifact changed during export")
                        archive.add(path, arcname=relative, recursive=False)
            return {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "run_id": self.run_id,
                "manifest_sha256": _sha(serialized),
                "artifact_count": len(manifest["artifacts"]),
                "artifact_ids": sorted(manifest["artifacts"]),
                "storage_bytes": self.quota().storage_bytes,
                "queries": manifest["query_count"],
                "returned_chars": manifest["returned_chars"],
            }

    def import_archive(self, stream: BinaryIO) -> dict:
        """Validate and atomically import an artifact archive into an empty store."""
        with self._lock:
            existing = self._load_manifest()
            if (existing["artifacts"] or existing.get("captures")
                    or existing["query_count"] or existing["returned_chars"]):
                raise ArtifactError("artifact archive can only restore into an empty store")
            staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=self.root))
            try:
                with tarfile.open(fileobj=stream, mode="r|") as archive:
                    first = archive.next()
                    if (
                        first is None
                        or first.name != "manifest.json"
                        or not first.isfile()
                        or first.size > 2 * 1024 * 1024
                    ):
                        raise ArtifactError("artifact archive has no bounded manifest header")
                    extracted = archive.extractfile(first)
                    if extracted is None:
                        raise ArtifactError("artifact archive manifest is unreadable")
                    manifest_bytes = extracted.read(2 * 1024 * 1024 + 1)
                    if len(manifest_bytes) != first.size:
                        raise ArtifactError("artifact archive manifest size changed")
                    try:
                        manifest = json.loads(manifest_bytes)
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        raise ArtifactError("artifact archive manifest is corrupt") from exc
                    if (
                        not isinstance(manifest, dict)
                        or bool(manifest.get("captures"))
                        or manifest.get("schema_version") != ARTIFACT_SCHEMA_VERSION
                        or manifest.get("run_id") != self.run_id
                        or manifest.get("redaction_policy_sha256") != self.redaction_policy_sha256
                        or not isinstance(manifest.get("artifacts"), dict)
                        or len(manifest["artifacts"]) > MAX_RUN_ARTIFACTS
                        or type(manifest.get("query_count")) is not int
                        or not 0 <= manifest["query_count"] <= MAX_SESSION_QUERIES
                        or type(manifest.get("returned_chars")) is not int
                        or not 0 <= manifest["returned_chars"] <= MAX_SESSION_RETURNED_CHARS
                    ):
                        raise ArtifactError("artifact archive belongs to another run or policy")
                    expected: dict[str, tuple[str, int]] = {}
                    for artifact_id, entry in manifest["artifacts"].items():
                        ref = self._entry_ref(entry)
                        if ref.artifact_id != artifact_id or ref.run_id != self.run_id:
                            raise ArtifactError("artifact archive reference identity is invalid")
                        if ref.redaction_policy_sha256 != self.redaction_policy_sha256:
                            raise ArtifactError("artifact archive redaction identity changed")
                        if set(entry.get("files", {})) != {
                            "raw_stdout",
                            "raw_stderr",
                            "view_stdout",
                            "view_stderr",
                        }:
                            raise ArtifactError("artifact archive file inventory is incomplete")
                        expected_paths = {
                            f"raw_{name}": f"objects/{artifact_id}/raw-{name}.bin"
                            for name in ("stdout", "stderr")
                        } | {
                            f"view_{name}": f"objects/{artifact_id}/view-{name}.txt"
                            for name in ("stdout", "stderr")
                        }
                        for field, value in entry["files"].items():
                            if (
                                not isinstance(value, list)
                                or len(value) != 3
                                or not isinstance(value[1], str)
                                or re.fullmatch(r"[a-f0-9]{64}", value[1]) is None
                                or type(value[2]) is not int
                                or value[2] < 0
                            ):
                                raise ArtifactError("artifact archive file receipt is invalid")
                            relative, digest, size = value
                            _validate_relative(relative)
                            if relative != expected_paths[field] or size > MAX_ARTIFACT_BYTES:
                                raise ArtifactError("artifact archive path or size is invalid")
                            if relative in expected:
                                raise ArtifactError("artifact archive paths are duplicated")
                            expected[relative] = (digest, size)
                        raw_count = sum(
                            entry["files"][f"raw_{name}"][2] for name in ("stdout", "stderr")
                        )
                        view_count = sum(
                            entry["files"][f"view_{name}"][2] for name in ("stdout", "stderr")
                        )
                        if raw_count != ref.raw_bytes or view_count != ref.view_bytes:
                            raise ArtifactError("artifact archive byte count is inconsistent")
                        if raw_count > MAX_ARTIFACT_BYTES or view_count > MAX_ARTIFACT_BYTES:
                            raise ArtifactError("artifact archive exceeds per-artifact limits")
                    if sum(size for _, size in expected.values()) > MAX_RUN_BYTES:
                        raise ArtifactError("artifact archive exceeds the run storage quota")
                    if len(expected) > MAX_RUN_ARTIFACTS * 4:
                        raise ArtifactError("artifact archive contains too many files")
                    seen: set[str] = set()
                    total = 0
                    while True:
                        member = archive.next()
                        if member is None:
                            break
                        _validate_relative(member.name)
                        if (
                            not member.isfile()
                            or member.issym()
                            or member.islnk()
                            or member.name in seen
                        ):
                            raise ArtifactError(
                                "artifact archive contains a link, special or duplicate member"
                            )
                        if member.name not in expected or member.size != expected[member.name][1]:
                            raise ArtifactError("artifact archive member differs from its manifest")
                        total += member.size
                        if total > MAX_RUN_BYTES:
                            raise ArtifactError("artifact archive exceeds the run storage quota")
                        source = archive.extractfile(member)
                        if source is None:
                            raise ArtifactError("artifact archive member cannot be read")
                        target = staging.joinpath(*PurePosixPath(member.name).parts)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        digest = hashlib.sha256()
                        remaining = member.size
                        with target.open("xb") as output:
                            while remaining:
                                chunk = source.read(min(64 * 1024, remaining))
                                if not chunk:
                                    raise ArtifactError("artifact archive member was truncated")
                                output.write(chunk)
                                digest.update(chunk)
                                remaining -= len(chunk)
                        if digest.hexdigest() != expected[member.name][0]:
                            raise ArtifactError("artifact archive member hash mismatch")
                        seen.add(member.name)
                    if seen != set(expected):
                        raise ArtifactError("artifact archive is missing one or more files")
                for _, entry in manifest["artifacts"].items():
                    ref = self._entry_ref(entry)
                    raw_parts = tuple(
                        (
                            name,
                            staging.joinpath(
                                *PurePosixPath(entry["files"][f"raw_{name}"][0]).parts
                            ).read_bytes(),
                        )
                        for name in ("stdout", "stderr")
                    )
                    view_parts = tuple(
                        (
                            name,
                            staging.joinpath(
                                *PurePosixPath(entry["files"][f"view_{name}"][0]).parts
                            ).read_bytes(),
                        )
                        for name in ("stdout", "stderr")
                    )
                    if (
                        _combined_sha(raw_parts) != ref.raw_sha256
                        or _combined_sha(view_parts) != ref.view_sha256
                    ):
                        raise ArtifactError("artifact combined digest does not match its receipt")
                expected_count = sum(
                    len(entry["files"]) for entry in manifest["artifacts"].values()
                )
                if len(seen) != expected_count:
                    raise ArtifactError("artifact archive inventory is inconsistent")
                moved: list[str] = []
                for artifact_id in manifest["artifacts"]:
                    os.replace(staging / "objects" / artifact_id, self.objects / artifact_id)
                    moved.append(artifact_id)
                self._save_manifest(manifest)
                return {
                    "manifest_sha256": _sha(manifest_bytes),
                    "artifact_count": len(manifest["artifacts"]),
                    "artifact_ids": sorted(manifest["artifacts"]),
                    "storage_bytes": sum(
                        info[2]
                        for entry in manifest["artifacts"].values()
                        for info in entry["files"].values()
                    ),
                    "queries": manifest["query_count"],
                    "returned_chars": manifest["returned_chars"],
                }
            except BaseException:
                for artifact_id in locals().get("moved", []):
                    shutil.rmtree(self.objects / artifact_id, ignore_errors=True)
                raise
            finally:
                shutil.rmtree(staging, ignore_errors=True)


def _validate_relative(value: str) -> None:
    if not isinstance(value, str):
        raise ArtifactError("artifact archive member path is not text")
    path = PurePosixPath(value)
    if (
        not isinstance(value, str)
        or path.is_absolute()
        or not path.parts
        or "\\" in value
        or any(part in {".", ".."} for part in path.parts)
        or not value.startswith("objects/")
    ):
        raise ArtifactError("artifact archive member path is unsafe")


def _tar_bytes(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    import io

    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    info.mtime = 0
    archive.addfile(info, io.BytesIO(data))


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
