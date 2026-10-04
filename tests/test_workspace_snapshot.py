"""Snapshot integrity and path bounds before any workspace restoration mutation."""

import copy
import hashlib
import io
import json
import tarfile

import pytest

from examples.replay_ordinary import _git
from tracefix.checkpoint import CheckpointError
from tracefix.workspace_snapshot import (
    export_workspace,
    restore_stream,
    restore_workspace,
    validate_archive,
)


@pytest.fixture
def saved(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "old.py").write_bytes(b"old\n")
    (source / "keep.py").write_bytes(b"base\n")
    _git(source, "init")
    _git(source, "add", ".")
    _git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-m", "base")
    (source / "old.py").unlink()
    (source / "keep.py").write_bytes(b"changed\n")
    (source / "product").mkdir()
    (source / "product/new.bin").write_bytes(b"\x00\xffdata")
    (source / ".tracefix-test-tmp").mkdir()
    (source / ".tracefix-test-tmp/transient").write_bytes(b"not a product")
    (source / "product/__pycache__").mkdir()
    (source / "product/__pycache__/temporary.pyc").write_bytes(b"cache")
    archive = tmp_path / "product.tar"
    with archive.open("wb") as stream:
        metadata = export_workspace(source, stream)
    return source, archive, metadata


def test_real_file_roundtrip_deletion_binary_products_and_cache_exclusion(saved, tmp_path):
    source, archive, metadata = saved
    assert metadata["deleted"] == ["old.py"]
    assert metadata["archive_bytes"] == archive.stat().st_size
    assert metadata["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    target = tmp_path / "restore"
    target.mkdir()
    (target / "old.py").write_bytes(b"baseline")
    (target / "extraneous.py").write_bytes(b"discard")
    (target / ".git").mkdir()
    (target / ".git/owned-baseline").write_bytes(b"preserve")
    restore_workspace(target, archive, metadata)
    assert (target / "keep.py").read_bytes() == b"changed\n"
    assert (target / "product/new.bin").read_bytes() == b"\x00\xffdata"
    assert not (target / "old.py").exists() and not (target / "extraneous.py").exists()
    assert not (target / ".tracefix-test-tmp").exists()
    assert not (target / "product/__pycache__").exists()
    assert (target / ".git/owned-baseline").read_bytes() == b"preserve"
    assert (source / ".tracefix-test-tmp/transient").exists()
    restore_workspace(target, archive, metadata)


@pytest.mark.parametrize("mode", [
    "schema", "files", "deleted", "bytes", "hash", "duplicate", "escape", "mode", "size",
    "content", "type", "missing", "deleted_escape",
])
def test_corrupt_metadata_is_rejected_before_target_changes(saved, tmp_path, mode):
    _, archive, original = saved
    metadata = copy.deepcopy(original)
    row = next(row for row in metadata["files"] if row["type"] == "file")
    if mode == "schema":
        metadata["schema_version"] = 99
    elif mode in {"files", "deleted"}:
        metadata[mode] = None
    elif mode == "bytes":
        metadata["archive_bytes"] += 1
    elif mode == "hash":
        metadata["archive_sha256"] = "0" * 64
    elif mode == "duplicate":
        metadata["files"].append(row.copy())
    elif mode == "escape":
        row["path"] = "../outside"
    elif mode == "mode":
        row["mode"] ^= 0o100
    elif mode == "size":
        row["size"] += 1
    elif mode == "content":
        row["sha256"] = "0" * 64
    elif mode == "type":
        row["type"] = "device"
    elif mode == "missing":
        metadata["files"].pop()
    else:
        metadata["deleted"] = ["C:/outside"]
    target = tmp_path / "target"
    target.mkdir()
    (target / "sentinel").write_bytes(b"untouched")
    with pytest.raises(CheckpointError):
        restore_workspace(target, archive, metadata)
    assert (target / "sentinel").read_bytes() == b"untouched"


def test_archive_corruption_and_both_size_preflights(saved):
    source, archive, metadata = saved
    with pytest.raises(CheckpointError, match="size"):
        validate_archive(archive, metadata, maximum=1)
    with pytest.raises(CheckpointError, match="limit"):
        export_workspace(source, io.BytesIO(), maximum=1)
    with pytest.raises(CheckpointError, match="limit"):
        export_workspace(source, io.BytesIO(), maximum=1024)
    archive.write_bytes(archive.read_bytes()[:-1] + b"X")
    with pytest.raises(CheckpointError, match="identity"):
        validate_archive(archive, metadata)


@pytest.mark.parametrize("target", ["../../outside", "/outside", "C:/outside", "a\\b"])
def test_symbolic_link_archive_cannot_escape(tmp_path, target):
    path = tmp_path / "links.tar"
    with tarfile.open(path, "w") as archive:
        link = tarfile.TarInfo("link")
        link.type, link.linkname, link.mode = tarfile.SYMTYPE, target, 0o777
        archive.addfile(link)
    metadata = {"schema_version": 1, "archive_bytes": path.stat().st_size,
                "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "deleted": [],
                "files": [{"path": "link", "type": "symlink", "target": target,
                           "mode": 0o777, "size": 0}]}
    with pytest.raises(CheckpointError, match="link"):
        validate_archive(path, metadata)


def test_snapshot_cli_export_and_restore(saved, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tracefix.workspace_snapshot import main

    source, _, _ = saved
    binary, messages = io.BytesIO(), io.StringIO()
    monkeypatch.setattr("sys.argv", ["snapshot", "export", "--workspace", str(source)])
    monkeypatch.setattr("sys.stdout", SimpleNamespace(buffer=binary))
    monkeypatch.setattr("sys.stderr", messages)
    assert main() == 0
    path, meta = tmp_path / "cli.tar", tmp_path / "cli.json"
    path.write_bytes(binary.getvalue())
    meta.write_text(messages.getvalue(), encoding="utf-8")
    target = tmp_path / "cli-restored"
    monkeypatch.setattr("sys.argv", ["snapshot", "restore", "--workspace", str(target),
                                     "--archive", str(path), "--metadata", str(meta)])
    assert main() == 0
    assert json.loads(meta.read_text())["deleted"] == ["old.py"]
    assert (target / "keep.py").read_bytes() == b"changed\n"


def test_streaming_restore_in_disposable_workspace_and_final_digest(saved, tmp_path):
    _, archive, metadata = saved
    target = tmp_path / "stream"
    target.mkdir()
    (target / "__pycache__").mkdir()
    (target / "__pycache__/cache.pyc").write_bytes(b"discard cache")
    restore_stream(target, io.BytesIO(archive.read_bytes()), metadata)
    assert (target / "keep.py").read_bytes() == b"changed\n"
    assert not (target / "__pycache__").exists()
    changed = copy.deepcopy(metadata)
    changed["archive_sha256"] = "0" * 64
    with pytest.raises(CheckpointError, match="identity"):
        restore_stream(tmp_path / "disposable-invalid", io.BytesIO(archive.read_bytes()), changed)


@pytest.mark.parametrize("fault,reason", [
    ("schema", "envelope"), ("limit", "envelope"), ("path", "product paths"),
    ("duplicate", "product paths"), ("missing", "unexpected"), ("mode", "permissions"),
    ("size", "size changed"), ("hash", "content changed"), ("type", "unsupported"),
])
def test_streaming_restore_rejects_corrupt_disposable_products(saved, tmp_path, fault, reason):
    _, archive, metadata = saved
    changed = copy.deepcopy(metadata)
    file = next(row for row in changed["files"] if row["type"] == "file")
    if fault == "schema":
        changed["schema_version"] = 2
    elif fault == "limit":
        changed["archive_bytes"] = 1024 ** 3 + 1
    elif fault == "path":
        file["path"] = "../outside"
    elif fault == "duplicate":
        changed["files"].append(copy.deepcopy(file))
    elif fault == "missing":
        changed["files"].remove(file)
    elif fault == "mode":
        file["mode"] ^= 0o100
    elif fault == "size":
        file["size"] += 1
    elif fault == "hash":
        file["sha256"] = "0" * 64
    else:
        file["type"] = "symlink"
    target = tmp_path / "disposable"
    with pytest.raises(CheckpointError, match=reason):
        restore_stream(target, io.BytesIO(archive.read_bytes()), changed)
    assert not (tmp_path / "outside").exists()
