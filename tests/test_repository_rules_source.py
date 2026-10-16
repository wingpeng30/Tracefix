from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.source import RuleSourceView


def write(root, name, body=b"guidance"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def symlink(target, link, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"native symbolic-link creation is unavailable: {type(exc).__name__}")


def test_only_repository_internal_guidance_is_read_and_absence_remains_none(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    write(tmp_path, "AGENTS.md", b"host ancestor must not be read")
    content = "仓库规则\r\n😀".encode()
    write(repo, "src/AGENTS.md", content)
    view = RuleSourceView(repo)
    assert view.read_bytes("AGENTS.md") is None
    assert view.read_bytes("src/AGENTS.md") == content
    assert view.read_bytes("missing/path/AGENTS.md") is None
    with pytest.raises(RuleError, match="unsupported_source"):
        view.read_bytes("README.md")


@pytest.mark.parametrize("name", ["../AGENTS.md", "/tmp/AGENTS.md", "C:/AGENTS.md",
    ".git/AGENTS.md", ".tracefix-test-tmp/AGENTS.md", ".tracefix/rules/../../secret.md"])
def test_lexical_rejection_precedes_opening_any_file(tmp_path, monkeypatch, name):
    view = RuleSourceView(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("rejected path reached file open")

    monkeypatch.setattr(os, "open", forbidden)
    with pytest.raises(RuleError):
        view.read_bytes(name)


def test_size_limits_are_checked_before_open_without_truncation(tmp_path, monkeypatch):
    write(tmp_path, "AGENTS.md", b"x" * 16385)
    write(tmp_path, ".tracefix/rules.toml", b"x" * 32769)
    view = RuleSourceView(tmp_path)
    calls = []
    real_open = os.open

    def observed(*args, **kwargs):
        calls.append(args[0])
        return real_open(*args, **kwargs)

    monkeypatch.setattr(os, "open", observed)
    for name in ("AGENTS.md", ".tracefix/rules.toml"):
        with pytest.raises(RuleError, match="limit"):
            view.read_bytes(name)
    assert not calls


@pytest.mark.parametrize("kind", ["file_link", "broken_link", "parent_link", "root_link"])
def test_native_links_are_refused_even_when_target_is_inside_the_repo(tmp_path, kind):
    repo, outside = tmp_path / "repo", tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    write(outside, "AGENTS.md", b"outside")
    name = "AGENTS.md"
    if kind == "root_link":
        link = tmp_path / "linked-root"
        symlink(repo, link, directory=True)
        with pytest.raises(RuleError):
            RuleSourceView(link)
        return
    if kind == "parent_link":
        symlink(outside, repo / "nested", directory=True)
        name = "nested/AGENTS.md"
    elif kind == "broken_link":
        symlink(outside / "missing.md", repo / name)
    else:
        target = write(repo, "actual.md")
        symlink(target, repo / name)
    with pytest.raises(RuleError, match="link_or_reparse"):
        RuleSourceView(repo).read_bytes(name)


def test_hardlinks_directories_and_non_directory_ancestors_are_not_rule_bodies(tmp_path):
    original = write(tmp_path, "original.md")
    os.link(original, tmp_path / "AGENTS.md")
    with pytest.raises(RuleError, match="not_regular"):
        RuleSourceView(tmp_path).read_bytes("AGENTS.md")
    (tmp_path / "AGENTS.md").unlink()
    (tmp_path / "AGENTS.md").mkdir()
    with pytest.raises(RuleError, match="not_regular"):
        RuleSourceView(tmp_path).read_bytes("AGENTS.md")
    write(tmp_path, "parent-file")
    with pytest.raises(RuleError, match="parent_not_directory"):
        RuleSourceView(tmp_path).read_bytes("parent-file/AGENTS.md")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="native POSIX FIFO contract")
def test_fifo_is_rejected_without_blocking_or_opening_it(tmp_path, monkeypatch):
    os.mkfifo(tmp_path / "AGENTS.md")
    view = RuleSourceView(tmp_path)
    monkeypatch.setattr(os, "open", lambda *args: pytest.fail("FIFO must never be opened"))
    with pytest.raises(RuleError, match="not_regular"):
        view.read_bytes("AGENTS.md")


@pytest.mark.skipif(os.name != "nt", reason="native Windows junction contract")
def test_native_junction_is_rejected_before_reading_its_body(tmp_path):
    repo, outside = tmp_path / "repo", tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    write(outside, "AGENTS.md", b"outside")
    link = repo / "linked"
    subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                   check=True, capture_output=True)
    with pytest.raises(RuleError, match="link_or_reparse"):
        RuleSourceView(repo).read_bytes("linked/AGENTS.md")


def test_a_raced_open_cannot_read_a_different_file_even_if_path_checks_were_valid(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    write(repo, "AGENTS.md", b"inside")
    outside = write(tmp_path, "private.md", b"PRIVATE outside body")
    view = RuleSourceView(repo)
    real_open, real_fdopen = os.open, os.fdopen
    reads, closed = [], []

    class Stream:
        def __init__(self, descriptor, mode):
            self.stream = real_fdopen(descriptor, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()
            closed.append(True)

        def fileno(self):
            return self.stream.fileno()

        def read(self, limit):
            reads.append(True)
            return self.stream.read(limit)

    monkeypatch.setattr(os, "open", lambda path, flags: real_open(outside, flags))
    monkeypatch.setattr(os, "fdopen", Stream)
    with pytest.raises(RuleError, match="changed_during_open"):
        view.read_bytes("AGENTS.md")
    assert not reads and closed == [True]


@pytest.mark.parametrize("mutation", ["metadata", "growth"])
def test_mutation_during_read_refuses_and_closes_handle(tmp_path, monkeypatch, mutation):
    path = write(tmp_path, "AGENTS.md")
    view = RuleSourceView(tmp_path)
    real_fdopen = os.fdopen
    before = path.stat()
    closed = []

    class Stream:
        def __init__(self, descriptor, mode):
            self.stream = real_fdopen(descriptor, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()
            closed.append(True)

        def fileno(self):
            return self.stream.fileno()

        def read(self, limit):
            if mutation == "growth":
                path.write_bytes(b"x" * 20000)
                return self.stream.read(limit)
            content = self.stream.read(limit)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 10_000_000_000))
            return content

    monkeypatch.setattr(os, "fdopen", Stream)
    with pytest.raises(RuleError, match="changed_during_read|limit_exceeded"):
        view.read_bytes("AGENTS.md")
    assert closed == [True]


def test_windows_cross_api_change_time_differs_but_identity_fields_must_match():
    from tracefix.repository_rules.file_io import _same_open_file

    fields = dict(st_dev=1, st_ino=2, st_mode=0o100644, st_nlink=1, st_size=5,
                  st_mtime_ns=10, st_ctime_ns=20, st_birthtime_ns=20)
    before = SimpleNamespace(**fields)
    opened = SimpleNamespace(**{**fields, "st_ctime_ns": 30})
    assert _same_open_file(before, opened, windows=True)
    assert not _same_open_file(before, opened, windows=False)
    for name in ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns",
                 "st_birthtime_ns"):
        changed = SimpleNamespace(**{**fields, name: fields[name] + 1})
        assert not _same_open_file(before, changed, windows=True), name


def test_handle_change_time_drift_with_unchanged_mtime_is_refused(tmp_path, monkeypatch):
    write(tmp_path, "AGENTS.md", b"before")
    view = RuleSourceView(tmp_path)
    actual_fstat = os.fstat
    reads = []

    def changed(descriptor):
        info = actual_fstat(descriptor)
        reads.append(True)
        if len(reads) == 1:
            return info
        fields = {name: getattr(info, name) for name in (
            "st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns",
            "st_ctime_ns")}
        return SimpleNamespace(**{**fields, "st_ctime_ns": info.st_ctime_ns + 1})

    monkeypatch.setattr(os, "fstat", changed)
    with pytest.raises(RuleError, match="changed_during_read"):
        view.read_bytes("AGENTS.md")
    assert len(reads) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows native lstat/fstat semantics")
def test_native_windows_repeated_overwrite_is_not_a_false_open_race(tmp_path):
    path = tmp_path / "AGENTS.md"
    view = RuleSourceView(tmp_path)
    for index in range(100):
        content = f"rules-{index}".encode()
        path.write_bytes(content)
        assert view.read_bytes("AGENTS.md") == content


def test_reparse_metadata_and_custom_evidence_domains_are_refused(tmp_path, monkeypatch):
    path = write(tmp_path, "nested/AGENTS.md")
    view = RuleSourceView(tmp_path, protected_dirs=("private-evidence",))
    with pytest.raises(RuleError, match="internal"):
        view.validate_target("PRIVATE-evidence/data.py")
    original = Path.lstat

    def junction(candidate, *args, **kwargs):
        result = original(candidate, *args, **kwargs)
        if candidate == path.parent:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result

    monkeypatch.setattr(Path, "lstat", junction)
    with pytest.raises(RuleError, match="link_or_reparse"):
        view.read_bytes("nested/AGENTS.md")


@pytest.mark.parametrize("scope", ["", "../data", "/outside", "C:/outside", "a//b"])
def test_invalid_protected_scope_fails_closed(tmp_path, scope):
    with pytest.raises(RuleError, match="invalid_protected_scope"):
        RuleSourceView(tmp_path, protected_dirs=(scope,))


def test_missing_root_file_root_and_io_failure_are_explicit(tmp_path, monkeypatch):
    with pytest.raises(RuleError, match="root_unavailable"):
        RuleSourceView(tmp_path / "missing")
    file = write(tmp_path, "plain-file")
    with pytest.raises(RuleError, match="root_not_regular"):
        RuleSourceView(file)
    write(tmp_path, "AGENTS.md")
    view = RuleSourceView(tmp_path)

    def denied(*args, **kwargs):
        raise PermissionError("injected")

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(RuleError, match="io_or_identity"):
        view.read_bytes("AGENTS.md")


def test_replaced_checkout_root_is_refused_before_opening_guidance(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    write(root, "AGENTS.md")
    view = RuleSourceView(root)
    root.rename(tmp_path / "old-repo")
    root.mkdir()
    write(root, "AGENTS.md", b"replacement")
    monkeypatch.setattr(os, "open", lambda *args: pytest.fail("changed root reached open"))
    with pytest.raises(RuleError, match="root_changed"):
        view.read_bytes("AGENTS.md")


@pytest.mark.parametrize("target", ["root", "file"])
def test_unverifiable_file_identity_is_rejected_without_reading(tmp_path, monkeypatch, target):
    path = write(tmp_path, "AGENTS.md")
    real_stat = Path.lstat

    def unknown_identity(candidate, *args, **kwargs):
        info = real_stat(candidate, *args, **kwargs)
        selected = tmp_path if target == "root" else path
        if candidate != selected:
            return info
        values = {name: getattr(info, name) for name in (
            "st_mode", "st_dev", "st_ino", "st_size", "st_nlink", "st_mtime_ns", "st_ctime_ns")}
        return SimpleNamespace(**{**values, "st_ino": 0})

    monkeypatch.setattr(Path, "lstat", unknown_identity)
    monkeypatch.setattr(os, "open", lambda *args: pytest.fail("unknown identity reached open"))
    with pytest.raises(RuleError, match="identity_unavailable"):
        RuleSourceView(tmp_path).read_bytes("AGENTS.md")


def test_parent_identity_change_after_read_cannot_publish_a_rule_body(tmp_path, monkeypatch):
    write(tmp_path, "AGENTS.md")
    view = RuleSourceView(tmp_path)
    original_stat, original_fdopen = Path.lstat, os.fdopen
    changed = False

    def parent_stat(candidate, *args, **kwargs):
        info = original_stat(candidate, *args, **kwargs)
        if candidate == tmp_path and changed:
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1,
                                   st_mode=info.st_mode)
        return info

    class Stream:
        def __init__(self, descriptor, mode):
            self.stream = original_fdopen(descriptor, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, limit):
            nonlocal changed
            content = self.stream.read(limit)
            changed = True
            return content

    monkeypatch.setattr(Path, "lstat", parent_stat)
    monkeypatch.setattr(os, "fdopen", Stream)
    with pytest.raises(RuleError, match="parent_changed"):
        view.read_bytes("AGENTS.md")
