"""Snapshot integrity and path bounds before any workspace restoration mutation."""

import copy
import hashlib
import io
import json
import os
import runpy
import tarfile
from types import SimpleNamespace

import pytest

from examples.replay_ordinary import _git
from tracefix.checkpoint import CheckpointError
from tracefix.workspace_snapshot import (
    _HashReader,
    _LimitedWriter,
    export_workspace,
    restore_stream,
    restore_workspace,
    safe_name,
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
    _git(
        source,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "base",
    )
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


def test_tracked_cache_named_products_survive_while_generated_files_are_excluded(saved, tmp_path):
    source, _, _ = saved
    cache = source / "product/__pycache__"
    (cache / "owned.py").write_bytes(b"user-owned product")
    _git(source, "add", "product/__pycache__/owned.py")
    archive = tmp_path / "tracked-cache.tar"
    with archive.open("wb") as stream:
        metadata = export_workspace(source, stream)
    paths = {row["path"] for row in metadata["files"]}
    assert "product/__pycache__/owned.py" in paths
    assert "product/__pycache__/temporary.pyc" not in paths
    target = tmp_path / "restored-cache"
    (target / "product/__pycache__").mkdir(parents=True)
    (target / "product/__pycache__/stale.pyc").write_bytes(b"old cache")
    (target / ".pytest_cache").mkdir()
    (target / ".pytest_cache/stale").write_text("old cache", encoding="utf-8")
    restore_workspace(target, archive, metadata)
    assert (target / "product/__pycache__/owned.py").read_bytes() == b"user-owned product"
    assert not (target / "product/__pycache__/stale.pyc").exists()
    assert not (target / ".pytest_cache").exists()
    assert not (target / "product/__pycache__/temporary.pyc").exists()


def test_snapshot_skips_linked_worktree_gitfile(saved, tmp_path):
    source, _, _ = saved
    worktree = tmp_path / "linked-worktree"
    _git(source, "worktree", "add", "--detach", str(worktree), "HEAD")
    git_file = worktree / ".git"
    assert git_file.is_file() and not git_file.is_dir()
    archive = io.BytesIO()
    metadata = export_workspace(worktree, archive)
    assert ".git" not in {row["path"] for row in metadata["files"]}


def test_snapshot_rejects_unsupported_product_file_kind(saved, monkeypatch):
    import tracefix.workspace_snapshot as snapshot

    source, _, _ = saved
    product = source / "keep.py"
    monkeypatch.setattr(snapshot, "_paths", lambda *_args, **_kwargs: [product])
    # Windows has no portable FIFO fixture, so exercise the file-type guard
    # by classifying this existing path as non-regular during archive export.
    monkeypatch.setattr(snapshot.stat, "S_ISREG", lambda _mode: False)
    with pytest.raises(CheckpointError, match="unsupported product file type"):
        export_workspace(source, io.BytesIO())


def test_snapshot_rejects_file_replacement_after_path_inspection(saved, monkeypatch):
    import tracefix.workspace_snapshot as snapshot

    source, _, _ = saved
    original = snapshot.os.fstat

    def replaced(descriptor):
        observed = original(descriptor)
        return SimpleNamespace(st_ino=observed.st_ino + 1, st_dev=observed.st_dev)

    monkeypatch.setattr(snapshot.os, "fstat", replaced)
    with pytest.raises(CheckpointError, match="changed during snapshot"):
        export_workspace(source, io.BytesIO())


def test_snapshot_rejects_product_metadata_change_after_open(saved, monkeypatch):
    import tracefix.workspace_snapshot as snapshot

    source, _, _ = saved
    original_fstat = snapshot.os.fstat
    changed = False

    def mutate_after_open(descriptor):
        nonlocal changed
        observed = original_fstat(descriptor)
        if not changed:
            product = source / "keep.py"
            current = product.stat()
            os.utime(product, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000))
            changed = True
            assert product.stat().st_mtime_ns != current.st_mtime_ns
        return observed

    monkeypatch.setattr(snapshot.os, "fstat", mutate_after_open)
    with pytest.raises(CheckpointError, match="changed during snapshot"):
        export_workspace(source, io.BytesIO())


@pytest.mark.parametrize(
    "target,member_under_link",
    [
        ("data.py", False),
        ("../outside", False),
        ("/outside", False),
        ("C:/outside", False),
        ("data.py", True),
    ],
)
def test_link_archive_validation_preserves_path_boundaries(tmp_path, target, member_under_link):
    archive = tmp_path / "links.tar"
    link = tarfile.TarInfo("link")
    link.type, link.linkname, link.mode = tarfile.SYMTYPE, target, 0o777
    records = [{"path": "link", "type": "symlink", "target": target, "mode": 0o777, "size": 0}]
    with tarfile.open(archive, "w") as stream:
        stream.addfile(link)
        if member_under_link:
            child = tarfile.TarInfo("link/child.py")
            child.mode = 0o644
            stream.addfile(child, io.BytesIO())
            records.append(
                {
                    "path": child.name,
                    "type": "file",
                    "mode": child.mode,
                    "size": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                }
            )
    metadata = {
        "schema_version": 1,
        "files": records,
        "deleted": [],
        "archive_bytes": archive.stat().st_size,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    if target == "data.py" and not member_under_link:
        assert validate_archive(archive, metadata)[0].issym()
        restored = tmp_path / "link-mirror"
        restore_workspace(restored, archive, metadata, materialize_links=False)
        assert not (restored / "link").exists()
    else:
        with pytest.raises(CheckpointError, match="link"):
            validate_archive(archive, metadata)


@pytest.mark.parametrize(
    "mode",
    [
        "schema",
        "files",
        "deleted",
        "bytes",
        "hash",
        "duplicate",
        "escape",
        "mode",
        "size",
        "content",
        "type",
        "missing",
        "deleted_escape",
    ],
)
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


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("", False),
        ("src/module.py", True),
        ("目录/文件.py", True),
        ("/absolute/file.py", False),
        ("C:relative.py", False),
        ("folder\\child.py", False),
        ("../outside.py", False),
        ("folder/../outside.py", False),
        ("folder/\x01name.py", False),
        ("folder/.GiT/config", False),
        ("folder/.TRACEFIX-TEST-TMP/output", False),
        ("folder//child.py", False),
        ("folder/./child.py", False),
    ],
)
def test_snapshot_path_names_are_relative_normalized_and_unprotected(name, expected):
    assert safe_name(name) is expected


def test_streaming_readers_and_writers_accept_exact_quota_and_reject_overflow():
    output = io.BytesIO()
    writer = _LimitedWriter(output, 3)
    assert writer.write(b"abc") == 3
    assert writer.size == 3
    assert writer.hash.hexdigest() == hashlib.sha256(b"abc").hexdigest()
    with pytest.raises(CheckpointError, match="limit"):
        writer.write(b"d")
    assert output.getvalue() == b"abc"

    reader = _HashReader(io.BytesIO(b"abc"), maximum=3)
    assert reader.read(2) == b"ab"
    assert reader.read() == b"c"
    assert reader.size == 3
    assert reader.hash.hexdigest() == hashlib.sha256(b"abc").hexdigest()
    with pytest.raises(CheckpointError, match="limit"):
        _HashReader(io.BytesIO(b"abc"), maximum=2).read()


@pytest.mark.parametrize("target", ["../../outside", "/outside", "C:/outside", "a\\b"])
def test_symbolic_link_archive_cannot_escape(tmp_path, target):
    path = tmp_path / "links.tar"
    with tarfile.open(path, "w") as archive:
        link = tarfile.TarInfo("link")
        link.type, link.linkname, link.mode = tarfile.SYMTYPE, target, 0o777
        archive.addfile(link)
    metadata = {
        "schema_version": 1,
        "archive_bytes": path.stat().st_size,
        "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "deleted": [],
        "files": [{"path": "link", "type": "symlink", "target": target, "mode": 0o777, "size": 0}],
    }
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
    monkeypatch.setattr(
        "sys.argv",
        [
            "snapshot",
            "restore",
            "--workspace",
            str(target),
            "--archive",
            str(path),
            "--metadata",
            str(meta),
        ],
    )
    assert main() == 0
    assert json.loads(meta.read_text())["deleted"] == ["old.py"]
    assert (target / "keep.py").read_bytes() == b"changed\n"


def test_empty_workspace_and_restore_stream_cli_roundtrip(saved, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tracefix.workspace_snapshot import main

    source, archive, metadata = saved
    empty = tmp_path / "empty-source"
    empty.mkdir()
    _git(empty, "init")
    empty_archive = io.BytesIO()
    empty_metadata = export_workspace(empty, empty_archive)
    empty_target = tmp_path / "empty-target"
    restore_stream(empty_target, io.BytesIO(empty_archive.getvalue()), empty_metadata)
    assert empty_target.is_dir()
    assert list(empty_target.iterdir()) == []

    empty_archive_path = tmp_path / "empty.tar"
    empty_archive_path.write_bytes(empty_archive.getvalue())
    disposable_target = tmp_path / "empty-restore-target"
    (disposable_target / ".git").mkdir(parents=True)
    (disposable_target / ".git" / "keep").write_text("git-owned", encoding="utf-8")
    (disposable_target / "remove-me.py").write_text("product", encoding="utf-8")
    restore_workspace(disposable_target, empty_archive_path, empty_metadata)
    assert list(path.name for path in disposable_target.iterdir()) == [".git"]
    assert (disposable_target / ".git" / "keep").read_text(encoding="utf-8") == "git-owned"

    metadata_path = tmp_path / "stream-metadata.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    target = tmp_path / "stream-cli-target"
    monkeypatch.setattr(
        "sys.argv",
        [
            "snapshot",
            "restore-stream",
            "--workspace",
            str(target),
            "--metadata",
            str(metadata_path),
        ],
    )
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(archive.read_bytes())))
    assert main() == 0
    assert (target / "keep.py").read_bytes() == b"changed\n"


@pytest.mark.parametrize("streamed", [False, True])
def test_restore_rechecks_resolved_parent_before_writing(saved, tmp_path, monkeypatch, streamed):
    _, archive, metadata = saved
    target = tmp_path / "restore-boundary"
    escaped_parent = target / "product"
    outside = tmp_path
    original_resolve = type(target).resolve

    def redirected_parent(path, *args, **kwargs):
        if path == escaped_parent:
            return original_resolve(outside, strict=True)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(type(target), "resolve", redirected_parent)
    message = "restore path escaped|streamed snapshot path escaped"
    with pytest.raises(CheckpointError, match=message):
        if streamed:
            restore_stream(target, io.BytesIO(archive.read_bytes()), metadata)
        else:
            restore_workspace(target, archive, metadata)
    assert not (outside / "product" / "new.bin").exists()


@pytest.mark.parametrize(
    ("operation", "has_metadata", "message"),
    [
        ("restore-stream", False, "restore-stream requires metadata"),
        ("restore", True, "restore requires archive and metadata"),
    ],
)
def test_snapshot_cli_rejects_incomplete_restore_arguments(
    tmp_path, monkeypatch, operation, has_metadata, message
):
    from tracefix.workspace_snapshot import main

    argv = ["snapshot", operation, "--workspace", str(tmp_path / "target")]
    if has_metadata:
        argv.extend(["--metadata", str(tmp_path / "metadata.json")])
    error_output = io.StringIO()
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr("sys.stderr", error_output)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert message in error_output.getvalue()


def test_snapshot_module_entrypoint_prints_help(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["tracefix.workspace_snapshot", "--help"])
    with pytest.warns(RuntimeWarning, match="found in sys.modules"):
        with pytest.raises(SystemExit) as result:
            runpy.run_module("tracefix.workspace_snapshot", run_name="__main__")
    assert result.value.code == 0
    assert "restore-stream" in capsys.readouterr().out


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


def test_streaming_restore_hashes_trailing_stream_bytes(saved, tmp_path):
    _, archive, metadata = saved
    stream = io.BytesIO(archive.read_bytes() + b"unexpected trailing bytes")
    with pytest.raises(CheckpointError, match="identity"):
        restore_stream(tmp_path / "disposable-with-tail", stream, metadata)


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("schema", "envelope"),
        ("limit", "envelope"),
        ("path", "product paths"),
        ("duplicate", "product paths"),
        ("missing", "unexpected"),
        ("mode", "permissions"),
        ("size", "size changed"),
        ("hash", "content changed"),
        ("type", "unsupported"),
    ],
)
def test_streaming_restore_rejects_corrupt_disposable_products(saved, tmp_path, fault, reason):
    _, archive, metadata = saved
    changed = copy.deepcopy(metadata)
    file = next(row for row in changed["files"] if row["type"] == "file")
    if fault == "schema":
        changed["schema_version"] = 2
    elif fault == "limit":
        changed["archive_bytes"] = 1024**3 + 1
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
