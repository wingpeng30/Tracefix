"""Stdlib-only bootstrap identity checks; also runnable with Python -I -S.

Capture is permitted only before project preparation, in an owned --init container
whose immutable Docker command has separately been verified as sleep infinity.
The resulting fixed kernel tokens are never rebuilt from a later process census.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path


def _process(pid: int, proc_root: Path) -> tuple[int, str, str]:
    fields = (proc_root / str(pid) / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
    parent, start = int(fields[1]), fields[19]
    if parent < 0 or not start.isdecimal():
        raise ValueError("invalid kernel process identity")
    return parent, start, fields[0]


def _inventory(proc_root: Path) -> set[tuple[int, str]]:
    tokens = set()
    for entry in proc_root.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            _, start, state = _process(int(entry.name), proc_root)
        except FileNotFoundError:
            continue
        if state != "Z":
            tokens.add((int(entry.name), start))
    return tokens


def capture_idle_tokens(proc_root: Path = Path("/proc")) -> tuple[tuple[int, str], ...]:
    """Initial trusted bootstrap only; unexpected processes are refused, never adopted."""
    if os.name != "posix":
        raise RuntimeError("docker_job_context_requires_linux")
    children = (proc_root / "1/task/1/children").read_text(encoding="ascii").split()
    if len(children) != 1 or not children[0].isdecimal():
        raise RuntimeError("docker_job_init_child_contract_changed")
    idle, own = int(children[0]), os.getpid()
    if idle <= 1 or own in {1, idle}:
        raise RuntimeError("docker_job_control_identity_invalid")
    init_parent, init_start, init_state = _process(1, proc_root)
    idle_parent, idle_start, idle_state = _process(idle, proc_root)
    own_parent, own_start, own_state = _process(own, proc_root)
    if (init_parent != 0 or idle_parent != 1 or own_parent != 0
            or "Z" in {init_state, idle_state, own_state}
            or (proc_root / str(idle) / "cmdline").read_bytes() != b"sleep\0infinity\0"):
        raise RuntimeError("docker_job_control_identity_invalid")
    fixed = ((1, init_start), (idle, idle_start))
    if _inventory(proc_root) != {*fixed, (own, own_start)}:
        raise RuntimeError("docker_job_preparation_contains_unowned_processes")
    return fixed


def bind_controller_tokens(fixed: tuple[tuple[int, str], ...],
                           proc_root: Path = Path("/proc")) -> frozenset[tuple[int, str]]:
    """Bind the current bridge to the previously captured init/idle kernel identities."""
    if (len(fixed) != 2 or len({pid for pid, _ in fixed}) != 2
            or fixed[0][0] != 1 or any(type(pid) is not int or pid <= 0
                                     or not start.isdecimal() for pid, start in fixed)):
        raise RuntimeError("docker_job_fixed_control_tokens_invalid")
    for pid, expected in fixed:
        _, actual, state = _process(pid, proc_root)
        if actual != expected or state == "Z":
            raise RuntimeError("docker_job_control_process_changed")
    own = os.getpid()
    parent, start, state = _process(own, proc_root)
    if own in {pid for pid, _ in fixed} or parent != 0 or state == "Z":
        raise RuntimeError("docker_job_bridge_identity_invalid")
    result = frozenset((*fixed, (own, start)))
    if _inventory(proc_root) != result:
        raise RuntimeError("docker_job_startup_contains_unowned_processes")
    return result


def read_root_context(path: Path) -> tuple[dict, bytes]:
    """Read fixed metadata without following links or trusting mutable task-owned files."""
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise RuntimeError("docker_job_context_path_is_linked")
    for parent in path.parents:
        directory = parent.lstat()
        if directory.st_uid != 0 or directory.st_mode & 0o022:
            raise RuntimeError("docker_job_context_directory_is_mutable")
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1
            or before.st_mode & 0o222 or before.st_size > 8192):
        raise RuntimeError("docker_job_context_not_root_readonly")
    raw = path.read_bytes()
    after = path.lstat()
    if (before.st_ino, before.st_dev, before.st_mtime_ns, before.st_size) != (
        after.st_ino, after.st_dev, after.st_mtime_ns, after.st_size
    ):
        raise RuntimeError("docker_job_context_changed_during_read")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("docker_job_context_is_not_an_object")
    return payload, raw


if __name__ == "__main__":
    print(json.dumps({"contract": "tracefix-docker-control-v1", "tokens": capture_idle_tokens()}))
