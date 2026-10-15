"""Kernel inventory contracts; real container ownership is checked by Docker replay."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.jobs import process_tree
from tracefix.jobs.domain import JobError


@pytest.fixture
def namespace(monkeypatch):
    controls = frozenset({(1, "init-start"), (2, "bridge-start")})
    current = set(controls)
    signals = []
    monkeypatch.setattr(process_tree, "os", SimpleNamespace(
        name="posix", getpgid=lambda pid: pid,
        killpg=lambda pid, sig: signals.append((pid, sig))))
    monkeypatch.setattr(process_tree, "signal", SimpleNamespace(SIGKILL=9))
    monkeypatch.setattr(process_tree, "_process_tokens", lambda: set(current))
    return controls, current, signals


def test_recycled_control_pid_cannot_be_adopted_as_a_trusted_process(namespace):
    controls, current, signals = namespace
    current.remove((2, "bridge-start"))
    current.add((2, "recycled-start"))
    with pytest.raises(JobError, match="unowned_processes"):
        process_tree.DockerProcessScope(controls)
    assert not signals


def test_killing_group_is_not_proof_that_escaped_descendants_exited(namespace, monkeypatch):
    controls, current, signals = namespace
    scope = process_tree.DockerProcessScope(controls)
    process = SimpleNamespace(pid=42, wait=lambda timeout: None)
    scope.process = process
    scope._attach(process)
    current.add((99, "escaped-descendant"))
    scope.terminate_tree()
    assert signals == [(42, 9)]
    assert not scope.wait_empty(timeout=0)
    # The container owner must abort; repeated close must never turn failure into success.
    monkeyclock = iter([0, 10])
    with monkeypatch.context() as bounded_clock:
        bounded_clock.setattr(process_tree.time, "monotonic", lambda: next(monkeyclock))
        assert not scope.close()
    current.discard((99, "escaped-descendant"))
    assert not scope.close() and not scope.wait_empty(timeout=0)
    before = list(signals)
    scope.terminate_tree()
    assert signals == before


def test_disappearing_group_can_still_finish_after_verified_empty_inventory(namespace, monkeypatch):
    controls, current, _ = namespace
    scope = process_tree.DockerProcessScope(controls)
    waited = []
    scope.process = SimpleNamespace(pid=42, wait=lambda timeout: waited.append(timeout))

    def vanished(pid, sig):
        raise ProcessLookupError("group already exited")

    monkeypatch.setattr(process_tree.os, "killpg", vanished)
    assert scope.close() and scope.close() and scope.wait_empty(timeout=0)
    assert waited == [2.0] and current == set(controls)


@pytest.mark.parametrize("fault", ["inventory", "kill_permission", "reap_timeout"])
def test_inventory_or_cleanup_failure_cannot_claim_success(namespace, monkeypatch, fault):
    controls, _, _ = namespace
    scope = process_tree.DockerProcessScope(controls)
    scope.process = SimpleNamespace(pid=42, wait=lambda timeout: None)
    if fault == "inventory":
        def unavailable():
            raise JobError("process_scope_inventory_unavailable")

        monkeypatch.setattr(process_tree, "_process_tokens", unavailable)
        assert not scope.wait_empty(timeout=0)
    elif fault == "kill_permission":
        def denied(pid, sig):
            raise PermissionError("group signal denied")

        monkeypatch.setattr(process_tree.os, "killpg", denied)
    else:
        def expired(timeout):
            raise subprocess.TimeoutExpired("owned gate", timeout)

        scope.process.wait = expired
        scope.process.kill = lambda: None
    assert not scope.close() and not scope.close()


def test_namespace_rejects_unowned_group_before_release(namespace, monkeypatch):
    controls, _, _ = namespace
    scope = process_tree.DockerProcessScope(controls)
    monkeypatch.setattr(process_tree.os, "getpgid", lambda pid: pid + 1)
    with pytest.raises(JobError, match="assignment_failed"):
        scope._attach(SimpleNamespace(pid=42))
    assert scope.close()


@pytest.mark.parametrize("backend", ["windows", "empty_controls"])
def test_unavailable_namespace_has_no_process_group_fallback(namespace, monkeypatch, backend):
    controls, _, signals = namespace
    if backend == "windows":
        monkeypatch.setattr(process_tree, "os", SimpleNamespace(name="nt"))
    else:
        controls = frozenset()
    with pytest.raises(JobError, match="scope_unavailable"):
        process_tree.DockerProcessScope(controls)
    assert not signals


def test_empty_domain_after_transient_descendant_is_confirmed(namespace, monkeypatch):
    controls, current, _ = namespace
    scope = process_tree.DockerProcessScope(controls)
    current.add((42, "test"))
    sleeps = []

    def drain(seconds):
        sleeps.append(seconds)
        current.clear()
        current.update(controls)

    monkeypatch.setattr(process_tree.time, "sleep", drain)
    assert scope.wait_empty(timeout=1) and sleeps == [0.01]
    assert scope.close()


def test_proc_inventory_ignores_zombies_and_disappearance_but_keeps_kernel_start_time(
    tmp_path, monkeypatch,
):
    root = tmp_path / "proc"
    root.mkdir()
    for name, state, start in (("20", "R", "start20"), ("10", "Z", "start10")):
        directory = root / name
        directory.mkdir()
        (directory / "stat").write_text(
            f"{name} (name (with) spaces) " + " ".join([state, *(["0"] * 18), start]))
    (root / "self").mkdir()
    (root / "30").mkdir()  # It exits between directory enumeration and reading stat.
    monkeypatch.setattr(process_tree, "Path", lambda value: root if value == "/proc"
                        else Path(value))
    assert process_tree._process_tokens() == {(20, "start20")}


@pytest.mark.parametrize("fault", ["truncated", "permission"])
def test_unreadable_proc_entry_cannot_be_treated_as_an_empty_namespace(
    tmp_path, monkeypatch, fault,
):
    root = tmp_path / "proc"
    directory = root / "20"
    directory.mkdir(parents=True)
    (directory / "stat").write_text("truncated")
    monkeypatch.setattr(process_tree, "Path", lambda value: root if value == "/proc"
                        else Path(value))
    if fault == "permission":
        original = Path.read_text

        def denied(path, *args, **kwargs):
            if path == directory / "stat":
                raise PermissionError("proc inaccessible")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(JobError, match="inventory_unavailable"):
        process_tree._process_tokens()
