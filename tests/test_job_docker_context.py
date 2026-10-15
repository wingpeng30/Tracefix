from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest

from tracefix.jobs import docker_context
from tracefix.jobs.docker_context import (
    bind_controller_tokens,
    capture_idle_tokens,
    read_root_context,
)


def process(root, pid, parent, start, *, state="S", command=b"python\0"):
    path = root / str(pid)
    path.mkdir(exist_ok=True)
    fields = [state, str(parent), *("0" for _ in range(17)), str(start)]
    (path / "stat").write_text(f"{pid} (controlled (name)) " + " ".join(fields), encoding="ascii")
    (path / "cmdline").write_bytes(command)


def initial(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    process(root, 1, 0, 100)
    process(root, 7, 1, 110, command=b"sleep\0infinity\0")
    process(root, 8, 0, 120)
    children = root / "1/task/1"
    children.mkdir(parents=True)
    (children / "children").write_text("7 ", encoding="ascii")
    monkeypatch.setattr(docker_context, "os", SimpleNamespace(name="posix", getpid=lambda: 8))
    return root


def test_only_initial_fixed_roles_are_trusted_and_a_later_bridge_has_its_own_kernel_token(
    tmp_path, monkeypatch,
):
    root = initial(tmp_path, monkeypatch)
    fixed = capture_idle_tokens(root)
    assert fixed == ((1, "100"), (7, "110"))
    (root / "8/stat").unlink()
    (root / "8/cmdline").unlink()
    (root / "8").rmdir()
    process(root, 9, 0, 130)
    process(root, 10, 9, 140, state="Z")
    monkeypatch.setattr(docker_context, "os", SimpleNamespace(name="posix", getpid=lambda: 9))
    assert bind_controller_tokens(fixed, root) == {(1, "100"), (7, "110"), (9, "130")}
    process(root, 11, 0, 150)
    with pytest.raises(RuntimeError, match="startup_contains_unowned"):
        bind_controller_tokens(fixed, root)
    with pytest.raises(RuntimeError, match="control_process_changed"):
        bind_controller_tokens(((1, "100"), (7, "999")), root)


@pytest.mark.parametrize("change,reason", [
    ("child_count", "init_child_contract_changed"),
    ("command", "control_identity_invalid"),
    ("parent", "control_identity_invalid"),
    ("zombie", "control_identity_invalid"),
    ("extra", "preparation_contains_unowned"),
])
def test_bootstrap_refuses_unexpected_initial_processes(tmp_path, monkeypatch, change, reason):
    root = initial(tmp_path, monkeypatch)
    if change == "child_count":
        (root / "1/task/1/children").write_text("7 8", encoding="ascii")
    elif change == "command":
        (root / "7/cmdline").write_bytes(b"not-sleep\0infinity\0")
    elif change == "parent":
        process(root, 7, 0, 110, command=b"sleep\0infinity\0")
    elif change == "zombie":
        process(root, 7, 1, 110, state="Z", command=b"sleep\0infinity\0")
    else:
        process(root, 15, 1, 200)
    with pytest.raises(RuntimeError, match=reason):
        capture_idle_tokens(root)


def test_context_requires_root_readonly_file_and_immutable_directory(tmp_path, monkeypatch):
    path = tmp_path / "context.json"
    path.write_text('{"contract":"fixture"}', encoding="utf-8")
    real_stat = type(path).lstat

    def attributes(self, *args, **kwargs):
        original = real_stat(self, *args, **kwargs)
        mode = stat.S_IFREG | 0o444 if self == path else stat.S_IFDIR | 0o755
        return SimpleNamespace(st_mode=mode, st_uid=0, st_nlink=1, st_ino=original.st_ino,
                               st_dev=original.st_dev, st_mtime_ns=original.st_mtime_ns,
                               st_size=original.st_size)

    monkeypatch.setattr(type(path), "lstat", attributes)
    data, raw = read_root_context(path)
    assert data == json.loads(raw) == {"contract": "fixture"}

    def writable_parent(self, *args, **kwargs):
        result = attributes(self, *args, **kwargs)
        if self == path.parent:
            result.st_mode |= 0o002
        return result

    monkeypatch.setattr(type(path), "lstat", writable_parent)
    with pytest.raises(RuntimeError, match="directory_is_mutable"):
        read_root_context(path)
