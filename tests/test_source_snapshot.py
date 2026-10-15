"""Filtered source snapshots are independently validated before Serena receives them."""

import copy
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from tracefix import source_snapshot
from tracefix.source_snapshot import (
    CommandSourceSnapshotProvider,
    WorkspaceSourceSnapshotProvider,
    _BoundedHashReader,
    _safe_source_name,
    export_source_snapshot,
)


def _git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_workspace_provider_filters_secrets_and_deleted_paths(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / ".env").write_text("API_KEY=must-not-export\n", encoding="utf-8")
    (workspace / ".env.example").write_text("API_KEY=example\n", encoding="utf-8")
    (workspace / ".tracefix-build-tmp").mkdir()
    (workspace / ".tracefix-build-tmp" / "internal.py").write_text("internal\n")
    _git(workspace, "init", "--quiet")
    _git(workspace, "add", "module.py", ".env.example", ".env", ".tracefix-build-tmp")
    _git(workspace, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-m", "base")
    (workspace / "module.py").unlink()
    (workspace / "added.py").write_text("NEW = True\n", encoding="utf-8")
    target = tmp_path / "snapshot"

    identity = WorkspaceSourceSnapshotProvider(workspace).materialize(target)

    assert (target / ".env.example").read_text(encoding="utf-8") == "API_KEY=example\n"
    assert (target / "added.py").read_text(encoding="utf-8") == "NEW = True\n"
    assert not (target / ".env").exists()
    assert not (target / ".tracefix-build-tmp").exists()
    assert identity.deleted == ("module.py",)
    assert identity.files == 2


def test_command_provider_streams_and_validates_executor_archive(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "module.py").write_text("VALUE = '你好'\n", encoding="utf-8")
    _git(workspace, "init", "--quiet")
    _git(workspace, "add", "module.py")
    _git(workspace, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-m", "base")
    target = tmp_path / "snapshot"
    source_root = Path(__file__).resolve().parents[1] / "src"
    provider = CommandSourceSnapshotProvider(
        (
            sys.executable,
            "-c",
            "import pathlib,sys; sys.path.insert(0,sys.argv[1]); "
            "from tracefix.source_snapshot import export_source_snapshot; "
            "export_source_snapshot(pathlib.Path(sys.argv[2]),sys.stdout.buffer)",
            str(source_root),
            str(workspace),
        )
    )

    identity = provider.materialize(target)

    assert identity.files == 1
    assert (target / "module.py").read_text(encoding="utf-8") == "VALUE = '你好'\n"
    assert (target / "module.py").stat().st_mode & 0o222 == 0


def test_archive_tampering_is_rejected_before_materialization(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(workspace, "init", "--quiet")
    _git(workspace, "add", "module.py")
    _git(workspace, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-m", "base")
    stream = io.BytesIO()
    export_source_snapshot(workspace, stream)
    archive_bytes = stream.getvalue()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        members = archive.getmembers()
        files = [(item, archive.extractfile(item).read()) for item in members]
    tampered = io.BytesIO()
    with tarfile.open(fileobj=tampered, mode="w:") as archive:
        for member, content in files:
            info = copy.copy(member)
            if info.name == "module.py":
                    content = b"VALUE = 2\r\n"
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    path = tmp_path / "tampered.tar"
    path.write_bytes(tampered.getvalue())
    from tracefix.source_snapshot import _validate_and_materialize

    destination = tmp_path / "rejected"
    destination.mkdir()
    with pytest.raises(ValueError, match="hash differs"):
        _validate_and_materialize(path, destination)
    assert list(destination.iterdir()) == []


def test_command_provider_applies_export_deadline(tmp_path):
    target = tmp_path / "snapshot"
    provider = CommandSourceSnapshotProvider(
        (sys.executable, "-c", "import time; time.sleep(10)"), timeout_seconds=0.1
    )
    with pytest.raises(TimeoutError, match="timed out"):
        provider.materialize(target)


def test_source_hash_is_stable_for_identical_contents(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "module.py").write_bytes(b"x\n")
    _git(workspace, "init", "--quiet")
    _git(workspace, "add", "module.py")
    _git(workspace, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-m", "base")
    first = tmp_path / "first"
    second = tmp_path / "second"
    left = WorkspaceSourceSnapshotProvider(workspace).materialize(first)
    right = WorkspaceSourceSnapshotProvider(workspace).materialize(second)
    assert left.sha256 == right.sha256
    assert left.sha256 == hashlib.sha256(
        b"module.py\0" + hashlib.sha256(b"x\n").digest()
    ).hexdigest()


@pytest.mark.parametrize(
    "name",
    [
        "",
        "../escape.py",
        "/absolute.py",
        "folder\\secret.py",
        ".ENV",
        "settings/.env.production",
        ".ssh/key",
        "src/.docker/config",
        "src/.tracefix-build-tmp/cache.py",
        "bad\nname.py",
        "./module.py",
    ],
)
def test_source_snapshot_path_filter_rejects_unsafe_or_private_names(name):
    assert not _safe_source_name(name)


@pytest.mark.parametrize("name", ["module.py", ".env.example", "src/package.json"])
def test_source_snapshot_path_filter_allows_expected_project_files(name):
    assert _safe_source_name(name)


def _source_archive(
    *,
    name="module.py",
    content=b"VALUE = 1\n",
    manifest=True,
    member_type=None,
    row_updates=None,
    manifest_updates=None,
):
    content_sha = hashlib.sha256(content).hexdigest()
    row = {"path": name, "size": len(content), "sha256": content_sha}
    if row_updates:
        row.update(row_updates)
    digest = hashlib.sha256(name.encode() + b"\0" + bytes.fromhex(content_sha)).hexdigest()
    manifest_value = {
        "schema_version": 1,
        "sha256": digest,
        "bytes": len(content),
        "files": [row],
        "deleted": [],
    }
    if manifest_updates:
        manifest_value.update(manifest_updates)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        if member_type is not None:
            info.type = member_type
            if member_type == tarfile.SYMTYPE:
                info.linkname = "../../outside"
                info.size = 0
            archive.addfile(info, io.BytesIO(content) if info.isfile() else None)
        else:
            archive.addfile(info, io.BytesIO(content))
        if manifest:
            raw = json.dumps(manifest_value, sort_keys=True, separators=(",", ":")).encode()
            info = tarfile.TarInfo("TRACEFIX-SOURCE-SNAPSHOT.json")
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    return output.getvalue()


@pytest.mark.parametrize(
    "fault, message",
    [
        ("missing-manifest", "duplicate or missing manifest"),
        ("unsafe-path", "unsafe path or special file"),
        ("link", "unsafe path or special file"),
        ("hash", "hash differs"),
        ("schema", "manifest schema is invalid"),
        ("membership", "membership differs"),
        ("row-size", "size differs"),
        ("row-hash", "hash is invalid"),
        ("deleted", "identity does not match"),
        ("total-bytes", "identity does not match"),
        ("nonempty", "new empty directory"),
    ],
)
def test_source_archive_validation_rejects_bad_members_before_writing(
    tmp_path, fault, message
):
    if fault == "missing-manifest":
        payload = _source_archive(manifest=False)
    elif fault == "unsafe-path":
        payload = _source_archive(name="../escape.py")
    elif fault == "link":
        payload = _source_archive(member_type=tarfile.SYMTYPE)
    elif fault == "hash":
        payload = _source_archive().replace(b"VALUE = 1\n", b"VALUE = 2\n")
    elif fault == "schema":
        payload = _source_archive(manifest_updates={"schema_version": 2})
    elif fault == "membership":
        payload = _source_archive(manifest_updates={"files": []})
    elif fault == "row-size":
        payload = _source_archive(row_updates={"size": 99})
    elif fault == "row-hash":
        payload = _source_archive(row_updates={"sha256": "invalid"})
    elif fault == "deleted":
        payload = _source_archive(manifest_updates={"deleted": ["../escape.py"]})
    elif fault == "total-bytes":
        payload = _source_archive(manifest_updates={"bytes": 99})
    else:
        payload = _source_archive()
    archive_path = tmp_path / f"{fault}.tar"
    archive_path.write_bytes(payload)
    destination = tmp_path / f"{fault}-destination"
    destination.mkdir()
    if fault == "nonempty":
        (destination / "occupied").write_text("preserve", encoding="utf-8")
    else:
        destination.rmdir()
        destination.mkdir()

    with pytest.raises(ValueError, match=message):
        source_snapshot._validate_and_materialize(archive_path, destination)

    if fault == "nonempty":
        assert (destination / "occupied").read_text(encoding="utf-8") == "preserve"
    else:
        assert list(destination.iterdir()) == []


def test_source_snapshot_enforces_file_and_archive_limits(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "module.py").write_bytes(b"long enough to exceed the test limit")
    _git(workspace, "init", "--quiet")
    _git(workspace, "add", "module.py")

    monkeypatch.setattr(source_snapshot, "MAX_SOURCE_BYTES", 4)
    with pytest.raises(ValueError, match="exceeds 100 MiB"):
        export_source_snapshot(workspace, io.BytesIO())

    monkeypatch.setattr(source_snapshot, "MAX_SOURCE_BYTES", 100)
    monkeypatch.setattr(source_snapshot, "MAX_SOURCE_FILES", 0)
    with pytest.raises(ValueError, match="exceeds 5,000 files"):
        export_source_snapshot(workspace, io.BytesIO())

    archive_path = tmp_path / "archive.tar"
    archive_path.write_bytes(_source_archive())
    destination = tmp_path / "archive-destination"
    destination.mkdir()
    monkeypatch.setattr(source_snapshot, "MAX_SOURCE_ARCHIVE_BYTES", 1)
    with pytest.raises(ValueError, match="transport limit"):
        source_snapshot._validate_and_materialize(archive_path, destination)

    monkeypatch.setattr(source_snapshot, "MAX_SOURCE_ARCHIVE_BYTES", 2**30)
    monkeypatch.setattr(source_snapshot, "MAX_SOURCE_FILES", 0)
    with pytest.raises(ValueError, match="exceeds 5,000 files"):
        source_snapshot._validate_and_materialize(archive_path, destination)

    monkeypatch.setattr(source_snapshot, "MAX_SOURCE_FILES", 5_000)
    monkeypatch.setattr(source_snapshot, "MAX_SOURCE_BYTES", 1)
    with pytest.raises(ValueError, match="exceeds 100 MiB"):
        source_snapshot._validate_and_materialize(archive_path, destination)


def test_source_archive_requires_a_regular_manifest(tmp_path):
    archive_path = tmp_path / "directory-manifest.tar"
    with tarfile.open(archive_path, "w:") as archive:
        info = tarfile.TarInfo("TRACEFIX-SOURCE-SNAPSHOT.json")
        info.type = tarfile.DIRTYPE
        archive.addfile(info)
    destination = tmp_path / "destination"
    destination.mkdir()

    with pytest.raises(ValueError, match="manifest is invalid"):
        source_snapshot._validate_and_materialize(archive_path, destination)


@pytest.mark.parametrize("fault", ["unsafe-row", "unreadable-root"])
def test_source_path_discovery_skips_paths_it_cannot_safely_resolve(tmp_path, monkeypatch, fault):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(workspace, "init", "--quiet")
    _git(workspace, "add", "module.py")
    if fault == "unsafe-row":
        monkeypatch.setattr(
            source_snapshot,
            "resolve_workspace_path",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                source_snapshot.UnsafeWorkspacePath("blocked")
            ),
        )
    else:
        original_lstat = Path.lstat

        def fail_file_lstat(path):
            if path.name == "module.py":
                raise OSError("file disappeared")
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", fail_file_lstat)
    assert source_snapshot._eligible_paths(workspace) == ((), ())


def test_source_snapshot_detects_inventory_changes_and_rejects_link_roots(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(workspace, "init", "--quiet")
    _git(workspace, "add", "module.py")
    selected = (("module.py",), ())

    calls = []

    def changing_inventory(_root):
        calls.append(None)
        return selected if len(calls) == 1 else (("other.py",), ())

    monkeypatch.setattr(
        source_snapshot,
        "_eligible_paths",
        changing_inventory,
    )
    with pytest.raises(ValueError, match="file list changed"):
        export_source_snapshot(workspace, io.BytesIO())

    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == workspace)
    with pytest.raises(ValueError, match="root cannot be a link"):
        export_source_snapshot(workspace, io.BytesIO())
    monkeypatch.setattr(Path, "is_symlink", original_is_symlink)


def test_bounded_hash_reader_rejects_source_growth():
    reader = _BoundedHashReader(io.BytesIO(b"too much"), remaining=3)

    with pytest.raises(ValueError, match="exceeds 100 MiB"):
        reader.read()


@pytest.mark.parametrize("fault", ["manifest", "file", "materialize"])
def test_source_archive_rejects_members_that_disappear_during_read(
    tmp_path, monkeypatch, fault
):
    archive_path = tmp_path / f"{fault}.tar"
    archive_path.write_bytes(_source_archive())
    destination = tmp_path / f"{fault}-snapshot"
    destination.mkdir()
    original_extract = tarfile.TarFile.extractfile
    calls = {"module.py": 0}

    def missing_content(archive, member):
        name = member.name if isinstance(member, tarfile.TarInfo) else member
        if fault == "manifest" and name == "TRACEFIX-SOURCE-SNAPSHOT.json":
            return None
        if fault == "file" and name == "module.py":
            return None
        if fault == "materialize" and name == "module.py":
            calls[name] += 1
            if calls[name] == 2:
                return None
        return original_extract(archive, member)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", missing_content)
    message = "manifest is unreadable" if fault == "manifest" else "content is unavailable"
    with pytest.raises(ValueError, match=message):
        source_snapshot._validate_and_materialize(archive_path, destination)
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("change", ["open", "read"])
def test_source_export_detects_a_file_changed_while_reading(tmp_path, monkeypatch, change):
    import types

    workspace = tmp_path / "repo"
    workspace.mkdir()
    source_file = workspace / "module.py"
    source_file.write_bytes(b"VALUE = 1\n")
    _git(workspace, "init", "--quiet")
    _git(workspace, "add", "module.py")
    real_fstat = source_snapshot.os.fstat
    original_lstat = Path.lstat
    lstat_calls = 0

    if change == "open":
        def changed_inode(descriptor):
            info = real_fstat(descriptor)
            return types.SimpleNamespace(
                st_ino=info.st_ino + 1, st_dev=info.st_dev, st_size=info.st_size
            )

        monkeypatch.setattr(source_snapshot.os, "fstat", changed_inode)
        message = "changed while its snapshot was opened"
    else:
        def changed_mtime(path, *args, **kwargs):
            nonlocal lstat_calls
            info = original_lstat(path, *args, **kwargs)
            if path == source_file:
                lstat_calls += 1
                if lstat_calls == 3:
                    return types.SimpleNamespace(
                        st_ino=info.st_ino,
                        st_dev=info.st_dev,
                        st_size=info.st_size,
                        st_mtime_ns=info.st_mtime_ns + 1,
                        st_mode=info.st_mode,
                        st_nlink=info.st_nlink,
                        st_file_attributes=getattr(info, "st_file_attributes", 0),
                    )
            return info

        monkeypatch.setattr(Path, "lstat", changed_mtime)
        message = "changed while its snapshot was read"

    with pytest.raises(ValueError, match=message):
        export_source_snapshot(workspace, io.BytesIO())


def test_command_source_provider_reports_exit_and_archive_overflow(tmp_path, monkeypatch):
    failed = CommandSourceSnapshotProvider(
        (sys.executable, "-c", "import sys; sys.stderr.write('export denied'); sys.exit(7)")
    )
    with pytest.raises(RuntimeError, match="export denied"):
        failed.materialize(tmp_path / "failed")

    monkeypatch.setattr(source_snapshot, "MAX_SOURCE_ARCHIVE_BYTES", 8)
    oversized = CommandSourceSnapshotProvider(
        (sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 1024)")
    )
    with pytest.raises(ValueError, match="transport limit"):
        oversized.materialize(tmp_path / "oversized")

    with pytest.raises(ValueError, match="fixed non-empty argument vector"):
        CommandSourceSnapshotProvider(("",))


def test_command_provider_keeps_only_the_stderr_tail(tmp_path):
    failed = CommandSourceSnapshotProvider(
        (
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('A' * 5000 + 'TAIL'); sys.exit(8)",
        )
    )
    with pytest.raises(RuntimeError) as error:
        failed.materialize(tmp_path / "stderr-tail")
    assert "TAIL" in str(error.value)
    assert "A" * 2500 not in str(error.value)


def test_command_provider_kills_a_process_when_wait_fails(tmp_path, monkeypatch):
    class EmptyPipe:
        def read(self, _size):
            return b""

    class Process:
        stdout = EmptyPipe()
        stderr = EmptyPipe()

        def __init__(self):
            self.returncode = None
            self.killed = False
            self.wait_calls = 0

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise OSError("wait failed")
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    process = Process()
    monkeypatch.setattr(source_snapshot.subprocess, "Popen", lambda *_a, **_kw: process)

    with pytest.raises(OSError, match="wait failed"):
        CommandSourceSnapshotProvider(("fixed-exporter",)).materialize(tmp_path / "failed-wait")

    assert process.killed
    assert process.wait_calls == 2


def test_source_snapshot_export_cli_writes_a_valid_archive(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "module.py").write_bytes(b"VALUE = 1\n")
    _git(workspace, "init", "--quiet")
    _git(workspace, "add", "module.py")
    output = io.BytesIO()
    monkeypatch.setattr(sys, "argv", ["tracefix-source-snapshot", "--export", str(workspace)])
    monkeypatch.setattr(sys, "stdout", type("BinaryOutput", (), {"buffer": output})())

    assert source_snapshot.main() == 0
    with tarfile.open(fileobj=io.BytesIO(output.getvalue()), mode="r:") as archive:
        assert "module.py" in archive.getnames()
