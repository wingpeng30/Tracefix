"""Fault contracts for platform adapters; real ownership is separately checked on each OS."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.jobs import process_tree
from tracefix.jobs.domain import JobError
from tracefix.jobs.process_tree import LinuxCgroupScope, ProcessStartError, ProcessTreeScope


@pytest.fixture
def kernel(tmp_path, monkeypatch):
    root = tmp_path / "delegated"
    root.mkdir()
    (root / "cgroup.controllers").write_text("pids", encoding="ascii")
    options = {"kill": True, "deny_create": False}
    real_mkdir, real_rmdir = Path.mkdir, Path.rmdir

    def mkdir(path, *args, **kwargs):
        if path.parent != root:
            return real_mkdir(path, *args, **kwargs)
        if options["deny_create"]:
            raise PermissionError("delegation revoked")
        real_mkdir(path, *args, **kwargs)
        for name, text in (("cgroup.procs", ""), ("cgroup.events", "populated 0\n")):
            (path / name).write_text(text, encoding="ascii")
        if options["kill"]:
            (path / "cgroup.kill").touch()

    def rmdir(path):
        if path.parent == root:
            for name in ("cgroup.procs", "cgroup.events", "cgroup.kill"):
                (path / name).unlink(missing_ok=True)
        real_rmdir(path)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "rmdir", rmdir)
    monkeypatch.setattr(process_tree, "Path", lambda value: root if str(value) == "/sys/fs/cgroup"
                        else Path(value))
    monkeypatch.setattr(process_tree, "os", SimpleNamespace(name="posix"))
    return root, options


def test_delegated_cgroup_contract_requires_empty_domain_before_release(kernel):
    root, _ = kernel
    scope = LinuxCgroupScope(root)
    scope._attach(SimpleNamespace(pid=42))
    assert (scope._leaf / "cgroup.procs").read_text() == "42"
    events = scope._leaf / "cgroup.events"
    events.write_text("populated 1\n", encoding="ascii")
    assert not scope.wait_empty(timeout=0)
    scope.terminate_tree()
    assert (scope._leaf / "cgroup.kill").read_text() == "1"
    events.write_text("populated 0\n", encoding="ascii")
    assert scope.wait_empty(timeout=0)
    assert scope.close() and not scope._leaf.exists()
    assert scope.close() and scope.wait_empty(timeout=0)
    scope.terminate_tree()


@pytest.mark.parametrize("fault", ["missing_events", "invalid_events", "populated", "kill_denied"])
def test_cgroup_fault_never_claims_tree_exit(kernel, monkeypatch, fault):
    root, _ = kernel
    scope = LinuxCgroupScope(root)
    events = scope._leaf / "cgroup.events"
    if fault == "missing_events":
        events.unlink()
    elif fault == "invalid_events":
        events.write_text("invalid\n", encoding="ascii")
    elif fault == "populated":
        events.write_text("populated 1\n", encoding="ascii")
    else:
        original = Path.write_text

        def denied(path, *args, **kwargs):
            if path == scope._leaf / "cgroup.kill":
                raise PermissionError("kill delegation revoked")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", denied)
    assert not scope.close()
    assert scope._closed and not scope._cleaned and scope._leaf.exists()
    assert not scope.close()


@pytest.mark.parametrize("fault", ["outside", "missing_controllers", "missing_kill", "deny_create"])
def test_unusable_delegation_has_no_process_group_fallback(kernel, fault):
    root, options = kernel
    selected = root
    if fault == "outside":
        selected = root.parent
    elif fault == "missing_controllers":
        (root / "cgroup.controllers").unlink()
    elif fault == "missing_kill":
        options["kill"] = False
    else:
        options["deny_create"] = True
    with pytest.raises(JobError, match="unavailable"):
        LinuxCgroupScope(selected)
    assert not list(root.glob("tracefix-job-*"))


class ContractScope(ProcessTreeScope):
    def _attach(self, process):
        pass

    def terminate_tree(self):
        pass

    def wait_empty(self, timeout=3):
        return True

    def close(self):
        self._closed = self._cleaned = True
        return True


@pytest.mark.parametrize("fault,started", [("nonce", None), ("pid_bool", None),
    ("extra", None), ("not_started", False), ("read_error", None), ("oversize", False),
    ("attach", False)])
def test_gate_failure_distinguishes_proven_no_start_from_missing_ack(
    tmp_path, monkeypatch, fault, started,
):
    payload = io.BytesIO()

    class Input(io.BytesIO):
        def close(self):
            payload.write(self.getvalue())
            super().close()

    class Output:
        def readline(self, limit):
            if fault == "read_error":
                raise OSError("lost ack pipe")
            request = json.loads(payload.getvalue())
            response = {"nonce": request["nonce"], "started": True, "pid": 10}
            if fault == "nonce":
                response["nonce"] = "wrong"
            elif fault == "pid_bool":
                response["pid"] = True
            elif fault == "extra":
                response["unexpected"] = "not allowed"
            elif fault == "not_started":
                response = {"nonce": request["nonce"], "started": False}
            return b"TRACEFIX_JOB_CONTROL:" + json.dumps(response).encode() + b"\n"

    process = SimpleNamespace(stdin=Input(), stdout=Output())
    calls = []

    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        return process

    monkeypatch.setattr(process_tree.subprocess, "Popen", spawn)
    scope = ContractScope()
    if fault == "attach":
        def reject(process):
            raise OSError("membership failed")
        scope._attach = reject
    argv = [sys.executable, "x" * 65536 if fault == "oversize" else "-c", "pass"]
    with pytest.raises(ProcessStartError) as error:
        scope.start(argv, cwd=tmp_path, env={})
    assert error.value.process_started is started and error.value.tree_exit_confirmed
    assert calls[0][0][1:3] == ["-I", "-S"] and calls[0][1]["shell"] is False
    assert Path(calls[0][0][0]) == Path(sys._base_executable).resolve()
    if fault in {"oversize", "attach"}:
        assert not payload.getvalue()
    with pytest.raises(JobError, match="already_dispatched"):
        scope.start(argv, cwd=tmp_path, env={})


@pytest.mark.parametrize("argv,timeout", [([], 5), ([sys.executable], 0)])
def test_invalid_scope_launch_does_not_create_even_the_trusted_gate(
    tmp_path, monkeypatch, argv, timeout,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid launch reached process creation")

    monkeypatch.setattr(process_tree.subprocess, "Popen", forbidden)
    with pytest.raises(JobError, match="invalid_launch"):
        ContractScope().start(argv, cwd=tmp_path, env={}, startup_timeout=timeout)


@pytest.mark.parametrize("interpreter", ["missing", "relative", "directory", "not_found"])
def test_unavailable_host_interpreter_never_falls_back_to_project_python(
    tmp_path, monkeypatch, interpreter,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("unverified interpreter reached process creation")

    value = {"missing": None, "relative": "python.exe", "directory": str(tmp_path),
             "not_found": str(tmp_path / "missing.exe")}[interpreter]
    monkeypatch.setattr(sys, "_base_executable", value)
    monkeypatch.setattr(process_tree.subprocess, "Popen", forbidden)
    scope = ContractScope()
    with pytest.raises(ProcessStartError) as failure:
        scope.start([str(tmp_path / "project-python.exe")], cwd=tmp_path, env={})
    assert failure.value.process_started is False and failure.value.tree_exit_confirmed
    assert scope._closed and scope.process is None
