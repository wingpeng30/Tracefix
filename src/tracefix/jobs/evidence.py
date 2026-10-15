"""Bounded audit export for tmpfs evidence; never follow links or extract archive paths."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

MAX_BYTES = 16 * 1024 * 1024
MAX_FILES = 1024
MAX_ENCODED_BYTES = 24 * 1024 * 1024


def safe_evidence_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (bool(path.parts) and path.as_posix() == name and not path.is_absolute()
            and not PureWindowsPath(name).drive and "\\" not in name
            and all(part not in {".", "..", ""} and ":" not in part
                    and part.rstrip(" .") == part
                    and not PureWindowsPath(part).is_reserved() for part in path.parts)
            and not any(ord(char) < 32 for char in name))


def _open_owned(root: Path, relative: str) -> int:
    if os.name == "nt":
        return os.open(root / relative, os.O_RDONLY | os.O_BINARY)
    # The production exporter runs inside Linux. Anchor each directory to its fd,
    # so a task cannot swap an ancestor for a symlink between validation and read.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(root, flags)
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)


def export(root: Path) -> bytes:
    rows, size, entries = [], 0, 0
    try:
        root_state = root.lstat()
    except FileNotFoundError as exc:
        raise ValueError("audit evidence root is missing or linked") from exc
    if (not stat.S_ISDIR(root_state.st_mode)
            or getattr(root_state, "st_file_attributes", 0) & 0x400):
        raise ValueError("audit evidence root is missing or linked")
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted((*dirs, *files)):
            entries += 1
            if entries > MAX_FILES * 2:
                raise ValueError("audit evidence directory quota exceeded")
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            before = path.lstat()
            if (not safe_evidence_name(relative) or stat.S_ISLNK(before.st_mode)
                    or getattr(before, "st_file_attributes", 0) & 0x400):
                raise ValueError("audit evidence contains a link or unsafe name")
            if stat.S_ISDIR(before.st_mode):
                continue
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("audit evidence contains a special or shared file")
            if len(rows) >= MAX_FILES or size + before.st_size > MAX_BYTES:
                raise ValueError("audit evidence quota exceeded")
            def identity(value):
                return (value.st_dev, value.st_ino, value.st_size,
                        value.st_mtime_ns, value.st_mode, value.st_nlink)

            descriptor = _open_owned(root, relative)
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if identity(before) != identity(opened):
                    raise ValueError("audit evidence changed during export")
                data = stream.read(min(MAX_BYTES - size, before.st_size) + 1)
            after = path.lstat()
            if (identity(before) != identity(opened) or identity(before) != identity(after)
                    or len(data) != before.st_size):
                raise ValueError("audit evidence changed during export")
            size += len(data)
            rows.append({"name": relative, "sha256": hashlib.sha256(data).hexdigest(),
                         "data": base64.b64encode(data).decode("ascii")})
    raw = json.dumps({"version": 1, "bytes": size,
                      "files": sorted(rows, key=lambda row: row["name"])},
                     separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_ENCODED_BYTES:
        raise ValueError("audit transfer exceeds encoded quota")
    return raw


def validate(raw: bytes) -> dict[str, bytes]:
    if len(raw) > MAX_ENCODED_BYTES:
        raise ValueError("audit transfer exceeds encoded quota")
    payload = json.loads(raw)
    if (not isinstance(payload, dict) or set(payload) != {"version", "bytes", "files"}
            or type(payload["version"]) is not int or payload["version"] != 1
            or type(payload["bytes"]) is not int
            or not isinstance(payload["files"], list) or len(payload["files"]) > MAX_FILES):
        raise ValueError("audit transfer manifest is invalid")
    result, names, size = {}, set(), 0
    for row in payload["files"]:
        if (not isinstance(row, dict) or set(row) != {"name", "sha256", "data"}
                or not isinstance(row["name"], str) or not safe_evidence_name(row["name"])
                or row["name"].casefold() in names):
            raise ValueError("audit transfer path is invalid or duplicated")
        data = base64.b64decode(row["data"], validate=True)
        size += len(data)
        if size > MAX_BYTES or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError("audit transfer size or content hash is invalid")
        names.add(row["name"].casefold())
        result[row["name"]] = data
    if size != payload["bytes"] or any(
        parent.as_posix().casefold() in names for name in result
        for parent in PurePosixPath(name).parents if parent.as_posix() != "."
    ):
        raise ValueError("audit transfer size or file/directory conflict")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    sys.stdout.buffer.write(export(parser.parse_args().root))
