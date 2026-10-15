from __future__ import annotations

import ctypes
import io
import json
import os
import sys
import time
import venv
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.jobs import process_tree
from tracefix.jobs.domain import JobError
from tracefix.jobs.process_tree import (
    DockerProcessScope,
    LinuxCgroupScope,
    ProcessStartError,
    ProcessTreeScope,
    WindowsJobScope,
)
from tracefix.security import minimal_subprocess_environment


def test_unsupported_scope_does_not_downgrade_to_a_process_group():
    with pytest.raises(JobError, match="unavailable"):
        LinuxCgroupScope(None)


def test_docker_scope_needs_trusted_inventory_and_rejects_unaccounted_processes(monkeypatch):
    monkeypatch.setattr(process_tree, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(process_tree, "_process_tokens", lambda: {(1, "init"), (22, "stray")})
    with pytest.raises(JobError, match="unowned"):
        DockerProcessScope(frozenset({(1, "init")}))
    scope = DockerProcessScope(frozenset({(1, "init"), (22, "stray")}))
    monkeypatch.setattr(process_tree, "_process_tokens", lambda: {(1, "init"), (22, "new-start")})
    assert not scope.wait_empty(timeout=0)
    monkeypatch.setattr(process_tree, "_process_tokens", lambda: {(1, "init")})
    assert scope.wait_empty(timeout=0)
    monkeypatch.setattr(process_tree, "_process_tokens", lambda: (_ for _ in ()).throw(
        JobError("process_scope_inventory_unavailable")))
    assert not scope.wait_empty(timeout=0)


@pytest.mark.parametrize("ack", [b"", b"workspace output\n", b"x" * 4097,
                                      b'TRACEFIX_JOB_CONTROL:{"started":true}\n'])
def test_unacknowledged_release_is_unknown_even_when_cleanup_succeeds(monkeypatch, tmp_path, ack):
    class Scope(ProcessTreeScope):
        def _attach(self, process):
            pass

        def terminate_tree(self):
            pass

        def wait_empty(self, timeout=3):
            return True

        def close(self):
            self._closed = self._cleaned = True
            return True

    process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(ack))
    monkeypatch.setattr(process_tree.subprocess, "Popen", lambda *args, **kwargs: process)
    scope = Scope()
    with pytest.raises(ProcessStartError) as error:
        scope.start([sys.executable, "-c", "pass"], cwd=tmp_path, env={})
    assert error.value.process_started is None
    assert error.value.tree_exit_confirmed


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object API")
def test_windows_parent_exit_does_not_release_its_live_descendants(tmp_path):
    scope = WindowsJobScope()
    command = (
        "import subprocess,sys; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
        "creationflags=512); print(p.pid,flush=True)"
    )
    try:
        process = scope.start([sys.executable, "-c", command], cwd=tmp_path,
                              env=minimal_subprocess_environment())
        assert int(process.stdout.readline()) > 0
        # The original test exits, but the inherited output pipe and Job still own its child.
        assert not scope.wait_empty(timeout=0.1)
        scope.terminate_tree()
        assert scope.wait_empty(timeout=3)
        process.communicate(timeout=3)
    finally:
        assert scope.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object API")
def test_windows_launch_gate_assigns_membership_before_test_code(tmp_path):
    marker = tmp_path / "test-code-ran"

    class CheckedScope(WindowsJobScope):
        def _attach(self, process):
            assert not marker.exists()
            super()._attach(process)

    scope = CheckedScope()
    try:
        process = scope.start(
            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
            cwd=tmp_path, env=minimal_subprocess_environment(),
        )
        assert process.communicate(timeout=10) == (b"", b"")
        assert marker.exists() and process.returncode == 0
        assert scope.process_started is True
        assert scope.wait_empty(timeout=2)
        with pytest.raises(JobError, match="already_dispatched"):
            scope.start([sys.executable], cwd=tmp_path, env={})
    finally:
        assert scope.close()
        assert scope.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows gate must exclude environment startup hooks")
def test_windows_gate_does_not_execute_site_extensions_before_assignment(tmp_path):
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    marker = tmp_path / "environment-hook-ran"
    site = environment / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (site / "startup.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n", encoding="utf-8")

    class DelayedAssignment(WindowsJobScope):
        def _attach(self, process):
            # Give the trusted gate time to initialize; environment hooks must stay dormant.
            time.sleep(0.3)
            assert not marker.exists()
            super()._attach(process)

    scope = DelayedAssignment()
    try:
        process = scope.start([str(environment / "Scripts" / "python.exe"), "-c", "pass"],
                              cwd=tmp_path, env=minimal_subprocess_environment())
        process.communicate(timeout=5)
        assert process.returncode == 0 and marker.exists()
    finally:
        assert scope.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object API")
def test_windows_venv_target_is_owned_even_when_assignment_is_delayed(tmp_path):
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False).create(environment)

    class DelayedAssignment(WindowsJobScope):
        def _attach(self, process):
            # A venv redirector may create its interpreter before its parent is assigned.
            time.sleep(0.3)
            super()._attach(process)

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.POINTER(ctypes.c_int)]
    kernel.IsProcessInJob.restype = ctypes.c_int
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    scope, handle = DelayedAssignment(), None
    try:
        process = scope.start(
            [str(environment / "Scripts/python.exe"), "-c",
             "import os,time; print(os.getpid(),flush=True); time.sleep(60)"],
            cwd=tmp_path, env=minimal_subprocess_environment())
        pid = int(process.stdout.readline())
        handle = kernel.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
        assert handle
        owned = ctypes.c_int()
        assert kernel.IsProcessInJob(handle, scope._job, ctypes.byref(owned))
        assert owned.value, "test interpreter escaped the Job through a venv redirector"
        scope.terminate_tree()
        assert scope.wait_empty(timeout=3)
        assert kernel.WaitForSingleObject(handle, 3000) == 0
        process.communicate(timeout=3)
    finally:
        # Keep the negative regression safe when run against the old implementation.
        if handle:
            kernel.TerminateProcess(handle, 1)
            kernel.WaitForSingleObject(handle, 3000)
            kernel.CloseHandle(handle)
        assert scope.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object API")
def test_windows_cancellation_stops_child_and_grandchild_in_new_process_groups(tmp_path):
    scope = WindowsJobScope()
    grandchild = "import time; time.sleep(60)"
    child = (
        "import subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c',{grandchild!r}],creationflags=512); "
        "print(p.pid,flush=True); time.sleep(60)"
    )
    parent = (
        "import subprocess,sys,time,os,json; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}],creationflags=512,"
        "stdout=subprocess.PIPE,text=True); "
        "print(json.dumps([os.getpid(),p.pid,int(p.stdout.readline())]),flush=True); time.sleep(60)"
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handles = []
    try:
        process = scope.start([sys.executable, "-c", parent], cwd=tmp_path,
                              env=minimal_subprocess_environment())
        # The test program emits a bounded ready record, independently of the trusted start ack.
        pids = json.loads(process.stdout.readline())
        for pid in pids:
            handle = kernel.OpenProcess(0x100000, False, pid)
            assert handle
            handles.append(handle)
        assert not scope.wait_empty(timeout=0)
        scope.terminate_tree()
        assert scope.wait_empty(timeout=3)
        process.communicate(timeout=3)
        assert all(kernel.WaitForSingleObject(handle, 3000) == 0 for handle in handles)
    finally:
        assert scope.close()
        for handle in handles:
            kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object API")
def test_windows_assignment_failure_cannot_execute_the_test(tmp_path):
    marker = tmp_path / "must-not-run"

    class RejectedScope(WindowsJobScope):
        def _attach(self, process):
            raise JobError("test_assignment_refused")

    scope = RejectedScope()
    with pytest.raises(ProcessStartError) as error:
        scope.start([sys.executable, "-c", f"open({str(marker)!r},'w').close()"],
                    cwd=tmp_path, env=minimal_subprocess_environment())
    assert error.value.process_started is False
    assert error.value.tree_exit_confirmed
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object API")
def test_windows_definite_child_start_failure_is_not_an_unknown_result(tmp_path):
    scope = WindowsJobScope()
    # The gate uses the known interpreter; its argv requests an invalid cwd for the child.
    with pytest.raises(ProcessStartError) as error:
        scope.start([sys.executable, "-c", "pass"], cwd=tmp_path / "missing",
                    env=minimal_subprocess_environment())
    assert error.value.process_started is False
    assert error.value.tree_exit_confirmed


@pytest.mark.skipif(os.name == "nt", reason="Linux delegated cgroup v2")
def test_linux_delegation_rejects_normal_filesystem_directory(tmp_path):
    with pytest.raises(JobError, match="unavailable"):
        LinuxCgroupScope(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="Linux delegated cgroup v2")
def test_linux_delegated_scope_kills_a_setsid_descendant(tmp_path):
    root = os.environ.get("TRACEFIX_TEST_CGROUP_ROOT")
    if not root:
        pytest.skip("positive cgroup probe needs explicit trusted host delegation")
    scope = LinuxCgroupScope(Path(root))
    command = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
        "start_new_session=True); print(p.pid,flush=True); time.sleep(60)"
    )
    try:
        process = scope.start([sys.executable, "-c", command], cwd=tmp_path,
                              env=minimal_subprocess_environment())
        assert int(process.stdout.readline()) > 0
        assert not scope.wait_empty(timeout=0)
        scope.terminate_tree()
        process.communicate(timeout=5)
        assert scope.wait_empty(timeout=3)
    finally:
        assert scope.close()
