"""Filtered, hash-verified source snapshots for isolated read-only analyzers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

from tracefix.security import UnsafeWorkspacePath, resolve_workspace_path

MAX_SOURCE_FILES = 5_000
MAX_SOURCE_BYTES = 100 * 1024 * 1024
MAX_SOURCE_ARCHIVE_BYTES = 112 * 1024 * 1024
_MANIFEST_NAME = "TRACEFIX-SOURCE-SNAPSHOT.json"


@dataclass(frozen=True, slots=True)
class SourceSnapshotIdentity:
    sha256: str
    files: int
    bytes: int
    deleted: tuple[str, ...]


class SourceSnapshotProvider(Protocol):
    """Materialize a filtered snapshot into a new, caller-owned directory."""

    def materialize(self, destination: Path) -> SourceSnapshotIdentity: ...


def _safe_source_name(name: str) -> bool:
    path = PurePosixPath(name)
    folded = {part.casefold() for part in path.parts}
    return (
        bool(name)
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in name
        and str(path) == name
        and not any(ord(char) < 32 for char in name)
        and not folded.intersection({
            ".git", ".aws", ".ssh", ".kube", ".docker", ".gnupg",
            ".tracefix-build-tmp", ".tracefix-test-tmp",
        })
        and not any(
            part.casefold() == ".env"
            or (part.casefold().startswith(".env.") and part.casefold() != ".env.example")
            for part in path.parts
        )
    )


def _git_paths(root: Path, *args: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z", *args],
        cwd=root,
        capture_output=True,
        timeout=20,
        check=True,
    )
    return tuple(os.fsdecode(item) for item in result.stdout.split(b"\0") if item)


def _eligible_paths(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    listed = _git_paths(root, "--cached", "--others", "--exclude-standard")
    selected: list[str] = []
    candidates = sorted(name for name in set(listed) if _safe_source_name(name))
    if len(candidates) > MAX_SOURCE_FILES:
        raise ValueError("Serena MCP source snapshot exceeds 5,000 files")
    for name in candidates:
        try:
            resolved = resolve_workspace_path(root, name, must_exist=True)
        except UnsafeWorkspacePath:
            continue
        path = root / name
        try:
            info = path.lstat()
        except OSError:
            continue
        if (
            stat.S_ISREG(info.st_mode)
            and info.st_nlink <= 1
            and not path.is_symlink()
            and resolved == path.resolve()
        ):
            selected.append(name)
    if len(selected) > MAX_SOURCE_FILES:
        raise ValueError("Serena MCP source snapshot exceeds 5,000 files")
    tracked = _git_paths(root, "--cached")
    deleted = tuple(
        name for name in sorted(set(tracked))
        if _safe_source_name(name) and not (root / name).exists() and not (root / name).is_symlink()
    )
    return tuple(selected), deleted


class _BoundedHashReader:
    def __init__(self, stream: BinaryIO, remaining: int) -> None:
        self.stream = stream
        self.remaining = remaining
        self.size = 0
        self.sha256 = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.size += len(data)
        if self.size > self.remaining:
            raise ValueError("Serena MCP source snapshot exceeds 100 MiB")
        self.sha256.update(data)
        return data


def export_source_snapshot(root: Path, output: BinaryIO) -> SourceSnapshotIdentity:
    """Stream only safe regular workspace files into a deterministic tar archive."""
    if root.is_symlink() or getattr(root.lstat(), "st_file_attributes", 0) & 0x400:
        raise ValueError("Serena MCP source root cannot be a link or junction")
    root = root.resolve(strict=True)
    names, deleted = _eligible_paths(root)
    digest = hashlib.sha256()
    files: list[dict[str, object]] = []
    total = 0
    with tarfile.open(fileobj=output, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        for name in names:
            path = root / name
            before = path.lstat()
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as source:
                observed = os.fstat(source.fileno())
                if (observed.st_ino, observed.st_dev, observed.st_size) != (
                    before.st_ino, before.st_dev, before.st_size
                ):
                    raise ValueError("Serena source changed while its snapshot was opened")
                if total + observed.st_size > MAX_SOURCE_BYTES:
                    raise ValueError("Serena MCP source snapshot exceeds 100 MiB")
                reader = _BoundedHashReader(source, MAX_SOURCE_BYTES - total)
                info = tarfile.TarInfo(name)
                info.size = observed.st_size
                info.mode = stat.S_IMODE(observed.st_mode) & 0o644
                info.mtime = 0
                archive.addfile(info, reader)
                after = path.lstat()
                if (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns) != (
                    before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns
                ) or reader.size != observed.st_size:
                    raise ValueError("Serena source changed while its snapshot was read")
            sha = reader.sha256.hexdigest()
            digest.update(name.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            digest.update(bytes.fromhex(sha))
            total += reader.size
            files.append({"path": name, "size": reader.size, "sha256": sha})
        if _eligible_paths(root) != (names, deleted):
            raise ValueError("Serena source file list changed during snapshot export")
        manifest = {
            "schema_version": 1,
            "sha256": digest.hexdigest(),
            "bytes": total,
            "files": files,
            "deleted": list(deleted),
        }
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        info = tarfile.TarInfo(_MANIFEST_NAME)
        info.size = len(encoded)
        info.mode = 0o444
        info.mtime = 0
        import io

        archive.addfile(info, io.BytesIO(encoded))
    return SourceSnapshotIdentity(digest.hexdigest(), len(files), total, deleted)


def _validate_and_materialize(archive_path: Path, destination: Path) -> SourceSnapshotIdentity:
    if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
        raise ValueError("source snapshot destination must be a new empty directory")
    if archive_path.stat().st_size > MAX_SOURCE_ARCHIVE_BYTES:
        raise ValueError("Serena MCP source archive exceeds its transport limit")
    with tarfile.open(archive_path, "r:") as archive:
        members = archive.getmembers()
        if len(members) > MAX_SOURCE_FILES + 1:
            raise ValueError("Serena MCP source snapshot exceeds 5,000 files")
        names = [member.name for member in members]
        if len(names) != len(set(names)) or names.count(_MANIFEST_NAME) != 1:
            raise ValueError("Serena source archive has duplicate or missing manifest entries")
        manifest_member = next(member for member in members if member.name == _MANIFEST_NAME)
        if not manifest_member.isfile() or manifest_member.size > 2 * 1024 * 1024:
            raise ValueError("Serena source manifest is invalid")
        manifest_stream = archive.extractfile(manifest_member)
        if manifest_stream is None:
            raise ValueError("Serena source manifest is unreadable")
        manifest = json.loads(manifest_stream.read().decode("utf-8"))
        if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list):
            raise ValueError("Serena source manifest schema is invalid")
        rows = manifest["files"]
        expected = {row.get("path"): row for row in rows if isinstance(row, dict)}
        file_members = [member for member in members if member.name != _MANIFEST_NAME]
        if (
            len(expected) != len(rows)
            or set(expected) != {member.name for member in file_members}
            or len(expected) > MAX_SOURCE_FILES
        ):
            raise ValueError("Serena source archive membership differs from its manifest")
        total = 0
        digest = hashlib.sha256()
        material: list[tuple[tarfile.TarInfo, dict[str, object]]] = []
        for member in file_members:
            row = expected[member.name]
            if not _safe_source_name(member.name) or not member.isfile():
                raise ValueError("Serena source archive contains an unsafe path or special file")
            if type(row.get("size")) is not int or row["size"] != member.size:
                raise ValueError("Serena source file size differs from its manifest")
            if not isinstance(row.get("sha256"), str) or len(row["sha256"]) != 64:
                raise ValueError("Serena source file hash is invalid")
            if total + member.size > MAX_SOURCE_BYTES:
                raise ValueError("Serena MCP source snapshot exceeds 100 MiB")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Serena source file content is unavailable")
            file_hash = hashlib.sha256()
            while chunk := stream.read(64 * 1024):
                file_hash.update(chunk)
            sha = file_hash.hexdigest()
            if sha != row["sha256"]:
                raise ValueError("Serena source file hash differs from its manifest")
            digest.update(member.name.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            digest.update(bytes.fromhex(sha))
            total += member.size
            material.append((member, row))
        deleted = manifest.get("deleted")
        if (
            total != manifest.get("bytes")
            or digest.hexdigest() != manifest.get("sha256")
            or not isinstance(deleted, list)
            or not all(isinstance(name, str) and _safe_source_name(name) for name in deleted)
        ):
            raise ValueError("Serena source snapshot identity does not match")
        for member, _row in material:
            target = destination / PurePosixPath(member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Serena source file content is unavailable")
            with target.open("xb") as output:
                shutil.copyfileobj(stream, output, length=64 * 1024)
            target.chmod(0o444)
    return SourceSnapshotIdentity(digest.hexdigest(), len(rows), total, tuple(deleted))


class WorkspaceSourceSnapshotProvider:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve(strict=True)

    def materialize(self, destination: Path) -> SourceSnapshotIdentity:
        destination.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix="tracefix-source-", suffix=".tar")
        os.close(descriptor)
        archive_path = Path(name)
        try:
            with archive_path.open("wb") as stream:
                export_source_snapshot(self.workspace, stream)
            return _validate_and_materialize(archive_path, destination)
        finally:
            archive_path.unlink(missing_ok=True)


class CommandSourceSnapshotProvider:
    """Stream a fixed executor command's tar output into a validated host snapshot."""

    def __init__(self, command: tuple[str, ...], *, timeout_seconds: float = 45) -> None:
        if not command or any(not isinstance(part, str) or not part for part in command):
            raise ValueError("source export command must be a fixed non-empty argument vector")
        self.command = command
        self.timeout_seconds = timeout_seconds

    def materialize(self, destination: Path) -> SourceSnapshotIdentity:
        destination.mkdir(parents=True, exist_ok=True)
        stderr_tail = bytearray()
        descriptor, name = tempfile.mkstemp(prefix="tracefix-source-", suffix=".tar")
        os.close(descriptor)
        archive_path = Path(name)
        try:
            with archive_path.open("wb") as stream:
                process = subprocess.Popen(
                    self.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                assert process.stdout is not None and process.stderr is not None
                stream_error: list[BaseException] = []
                size = 0

                def drain_stdout() -> None:
                    nonlocal size
                    try:
                        while chunk := process.stdout.read(64 * 1024):
                            size += len(chunk)
                            if size > MAX_SOURCE_ARCHIVE_BYTES:
                                raise ValueError(
                                    "Serena MCP source archive exceeds its transport limit"
                                )
                            stream.write(chunk)
                    except BaseException as exc:
                        stream_error.append(exc)
                        process.kill()

                def drain_stderr() -> None:
                    while chunk := process.stderr.read(4096):
                        stderr_tail.extend(chunk)
                        if len(stderr_tail) > 4096:
                            del stderr_tail[:-4096]

                stderr_reader = threading.Thread(target=drain_stderr, daemon=True)
                stdout_reader = threading.Thread(target=drain_stdout, daemon=True)
                stderr_reader.start()
                stdout_reader.start()
                try:
                    try:
                        returncode = process.wait(timeout=self.timeout_seconds)
                    except subprocess.TimeoutExpired as exc:
                        process.kill()
                        process.wait()
                        raise TimeoutError("Serena source export timed out") from exc
                except BaseException:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    raise
                finally:
                    stdout_reader.join(timeout=2)
                    stderr_reader.join(timeout=2)
                if stream_error:
                    raise stream_error[0]
            if returncode != 0:
                raise RuntimeError(
                    "Serena source exporter failed: "
                    + bytes(stderr_tail).decode("utf-8", "replace")[-2000:]
                )
            return _validate_and_materialize(archive_path, destination)
        finally:
            archive_path.unlink(missing_ok=True)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--export", type=Path, required=True)
    args = parser.parse_args()
    export_source_snapshot(args.export, sys.stdout.buffer)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised in the real container job
    raise SystemExit(main())
