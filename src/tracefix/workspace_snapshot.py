"""Bounded product snapshots, shared by the host and ordinary Linux task container."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO

from tracefix.checkpoint import CheckpointError

MAX_SNAPSHOT_BYTES = 1024 ** 3
_PROTECTED = {".git", ".tracefix-build-tmp", ".tracefix-test-tmp"}
_CACHES = {"__pycache__", ".pytest_cache"}


def safe_name(name: str) -> bool:
    parts = PurePosixPath(name).parts
    windows = PureWindowsPath(name)
    return (bool(parts) and not name.startswith("/") and "\\" not in name
            and not windows.drive and not windows.root and ".." not in parts
            and not any(ord(char) < 32 for char in name)
            and not any(part.casefold() in _PROTECTED for part in parts)
            and str(PurePosixPath(name)) == name)


def _paths(root: Path) -> list[Path]:
    paths = []
    for directory, directories, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        directories[:] = sorted(name for name in directories
                                 if name.casefold() not in _PROTECTED | _CACHES)
        for name in sorted([*directories, *files]):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if not safe_name(relative) or not path.resolve().is_relative_to(root):
                raise CheckpointError("snapshot path or link escaped the workspace")
            paths.append(path)
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


class _LimitedWriter:
    def __init__(self, stream: BinaryIO, maximum: int) -> None:
        self.stream, self.maximum, self.size = stream, maximum, 0
        self.hash = hashlib.sha256()

    def write(self, data: bytes) -> int:
        if self.size + len(data) > self.maximum:
            raise CheckpointError("workspace snapshot exceeds the 1 GiB limit")
        self.stream.write(data)
        self.hash.update(data)
        self.size += len(data)
        return len(data)


class _HashReader:
    def __init__(self, stream: BinaryIO, maximum: int | None = None) -> None:
        self.stream = stream
        self.hash = hashlib.sha256()
        self.size, self.maximum = 0, maximum

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.size += len(data)
        if self.maximum is not None and self.size > self.maximum:
            raise CheckpointError("workspace snapshot exceeds the 1 GiB limit")
        self.hash.update(data)
        return data


def export_workspace(root: Path, stream: BinaryIO, *, maximum: int = MAX_SNAPSHOT_BYTES
                     ) -> dict[str, Any]:
    """Stream one complete snapshot without duplicating its bytes in container tmpfs."""
    root = root.resolve(strict=True)
    paths = _paths(root)
    if sum(path.lstat().st_size for path in paths if path.is_file()
           and not path.is_symlink()) > maximum:
        raise CheckpointError("workspace snapshot exceeds the 1 GiB limit")
    writer = _LimitedWriter(stream, maximum)
    records = []
    with tarfile.open(fileobj=writer, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        for path in paths:
            before = path.lstat()
            name = path.relative_to(root).as_posix()
            item = tarfile.TarInfo(name)
            item.mode = stat.S_IMODE(before.st_mode) & 0o777
            record: dict[str, Any] = {"path": name, "mode": item.mode, "size": 0}
            if path.is_symlink():
                target = os.readlink(path)
                if not (path.parent / target).resolve().is_relative_to(root):
                    raise CheckpointError("snapshot link escaped the workspace")
                item.type, item.linkname = tarfile.SYMTYPE, target
                record.update(type="symlink", target=target)
                archive.addfile(item)
            elif stat.S_ISDIR(before.st_mode):
                item.type = tarfile.DIRTYPE
                record["type"] = "directory"
                archive.addfile(item)
            elif stat.S_ISREG(before.st_mode):
                item.size = before.st_size
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as source:
                    observed = os.fstat(source.fileno())
                    if (observed.st_ino, observed.st_dev) != (before.st_ino, before.st_dev):
                        raise CheckpointError("workspace changed during snapshot")
                    reader = _HashReader(source)
                    archive.addfile(item, reader)
                    record.update(type="file", size=item.size, sha256=reader.hash.hexdigest())
                after = path.lstat()
                if (after.st_size, after.st_mtime_ns, after.st_mode) != (
                    before.st_size, before.st_mtime_ns, before.st_mode,
                ):
                    raise CheckpointError("workspace changed during snapshot")
            else:
                raise CheckpointError("snapshot contains an unsupported product file type")
            records.append(record)
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True,
                             capture_output=True).stdout.decode("utf-8").split("\0")
    deleted = sorted(name for name in tracked if name and not (root / name).exists()
                     and not (root / name).is_symlink())
    if any(not safe_name(name) for name in deleted):
        raise CheckpointError("deleted product path is unsafe")
    return {"schema_version": 1, "archive_sha256": writer.hash.hexdigest(),
            "archive_bytes": writer.size, "files": records, "deleted": deleted}


def validate_archive(path: Path, metadata: dict[str, Any], *,
                     maximum: int = MAX_SNAPSHOT_BYTES) -> list[tarfile.TarInfo]:
    """Validate every member and its content before any restoration mutation."""
    if (metadata.get("schema_version") != 1 or not isinstance(metadata.get("files"), list)
            or not isinstance(metadata.get("deleted"), list) or path.is_symlink()
            or path.stat().st_size > maximum or path.stat().st_size != metadata["archive_bytes"]):
        raise CheckpointError("invalid workspace snapshot envelope or size")
    with path.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != metadata["archive_sha256"]:
            raise CheckpointError("workspace snapshot archive identity changed")
    records = {row["path"]: row for row in metadata["files"]}
    if (len(records) != len(metadata["files"])
            or any(not safe_name(name) for name in [*records, *metadata["deleted"]])):
        raise CheckpointError("invalid or duplicate snapshot product path")
    links = {name for name, row in records.items() if row.get("type") == "symlink"}
    with tarfile.open(path, "r:") as archive:
        members = archive.getmembers()
        if len(members) != len(records) or {item.name for item in members} != set(records):
            raise CheckpointError("snapshot member set does not match metadata")
        for item in members:
            row = records[item.name]
            parents = PurePosixPath(item.name).parents
            if any(str(parent) in links for parent in parents):
                raise CheckpointError("snapshot member traverses a symbolic link")
            if item.mode != row["mode"] or not 0 <= item.mode <= 0o777 or item.size != row["size"]:
                raise CheckpointError("snapshot member permissions or size changed")
            if row["type"] == "file" and item.isfile():
                source = archive.extractfile(item)
                if (source is None or hashlib.file_digest(source, "sha256").hexdigest()
                        != row["sha256"]):
                    raise CheckpointError("snapshot product content changed")
            elif row["type"] == "directory" and item.isdir():
                pass
            elif row["type"] == "symlink" and item.issym():
                target = row["target"]
                combined = PurePosixPath(item.name).parent / target
                depth = 0
                for part in combined.parts:
                    depth += -1 if part == ".." else 0 if part == "." else 1
                    if depth < 0:
                        raise CheckpointError("snapshot link escaped the workspace")
                if (item.linkname != target or PurePosixPath(target).is_absolute()
                        or PureWindowsPath(target).drive or "\\" in target):
                    raise CheckpointError("snapshot symbolic link identity is unsafe")
            else:
                raise CheckpointError("unsupported snapshot member type")
    return members


def _clear_products(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve(strict=True)
    for name in _CACHES:
        cache = root / name
        if cache.is_symlink():
            cache.unlink()
        elif cache.is_dir():
            if not cache.resolve().is_relative_to(root):
                raise CheckpointError("cache path escaped the workspace")
            shutil.rmtree(cache)
    current = _paths(root)
    for path in current:
        if not path.is_symlink():
            os.chmod(path, 0o700)
    for path in reversed(current):
        if path.is_dir() and not path.is_symlink():
            # Cache directories are disposable, never an external link or repository metadata.
            for cache in _CACHES:
                target = path / cache
                if target.is_dir() and not target.is_symlink():
                    if not target.resolve().is_relative_to(root):
                        raise CheckpointError("cache path escaped the workspace")
                    shutil.rmtree(target)
            path.rmdir()
        else:
            path.unlink()
    return root


def restore_workspace(root: Path, archive_path: Path, metadata: dict[str, Any], *,
                      materialize_links: bool = True) -> None:
    """Replace only validated product paths; preserve Git and rebuild tool temp state elsewhere."""
    members = validate_archive(archive_path, metadata)
    root = _clear_products(root)
    with tarfile.open(archive_path, "r:") as archive:
        for item in members:
            target = root / item.name
            if not target.parent.resolve().is_relative_to(root):
                raise CheckpointError("restore path escaped the workspace")
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.isdir():
                target.mkdir(exist_ok=True)
            elif item.issym():
                if materialize_links:
                    target.symlink_to(item.linkname)
            else:
                source = archive.extractfile(item)
                assert source is not None
                with target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                os.chmod(target, item.mode)
        for item in reversed(members):
            if item.isdir():
                os.chmod(root / item.name, item.mode)


def restore_stream(root: Path, stream: BinaryIO, metadata: dict[str, Any]) -> None:
    """Restore a host-validated stream in a disposable new container, then verify all bytes."""
    if (metadata.get("schema_version") != 1
            or not 0 <= metadata["archive_bytes"] <= MAX_SNAPSHOT_BYTES):
        raise CheckpointError("invalid streamed snapshot envelope")
    records = {row["path"]: row for row in metadata["files"]}
    if (len(records) != len(metadata["files"])
            or any(not safe_name(name) for name in records)):
        raise CheckpointError("invalid streamed snapshot product paths")
    root = _clear_products(root)
    reader = _HashReader(stream, MAX_SNAPSHOT_BYTES)
    seen = set()
    directories = []
    with tarfile.open(fileobj=reader, mode="r|") as archive:
        for item in archive:
            if item.name in seen or item.name not in records:
                raise CheckpointError("unexpected streamed snapshot member")
            row = records[item.name]
            if (item.mode != row["mode"] or item.size != row["size"]
                    or not 0 <= item.mode <= 0o777):
                raise CheckpointError("streamed snapshot permissions or size changed")
            target = root / item.name
            if not target.parent.resolve().is_relative_to(root):
                raise CheckpointError("streamed snapshot path escaped the workspace")
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.isdir() and row["type"] == "directory":
                target.mkdir(exist_ok=True)
                directories.append((target, item.mode))
            elif item.issym() and row["type"] == "symlink":
                if item.linkname != row["target"] or not (
                    target.parent / item.linkname
                ).resolve().is_relative_to(root):
                    raise CheckpointError("streamed snapshot link escaped the workspace")
                target.symlink_to(item.linkname)
            elif item.isfile() and row["type"] == "file":
                source = archive.extractfile(item)
                assert source is not None
                digest = hashlib.sha256()
                with target.open("wb") as destination:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        destination.write(chunk)
                if digest.hexdigest() != row["sha256"]:
                    raise CheckpointError("streamed product content changed")
                os.chmod(target, item.mode)
            else:
                raise CheckpointError("unsupported streamed snapshot member")
            seen.add(item.name)
    while reader.read(1024 * 1024):
        pass
    if (seen != set(records) or reader.size != metadata["archive_bytes"]
            or reader.hash.hexdigest() != metadata["archive_sha256"]):
        raise CheckpointError("streamed snapshot identity changed")
    for directory, mode in reversed(directories):
        os.chmod(directory, mode)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("export", "restore", "restore-stream"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--metadata", type=Path)
    args = parser.parse_args()
    if args.operation == "export":
        metadata = export_workspace(args.workspace, sys.stdout.buffer)
        sys.stdout.buffer.flush()
        sys.stderr.write(json.dumps(metadata, ensure_ascii=True))
    elif args.operation == "restore-stream":
        if args.metadata is None:
            parser.error("restore-stream requires metadata")
        restore_stream(args.workspace, sys.stdin.buffer,
                       json.loads(args.metadata.read_text(encoding="utf-8")))
    else:
        if args.archive is None or args.metadata is None:
            parser.error("restore requires archive and metadata")
        restore_workspace(args.workspace, args.archive,
                          json.loads(args.metadata.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
