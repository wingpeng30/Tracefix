"""Injected Windows API failures must preserve ownership and cleanup uncertainty."""

from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from tracefix.jobs import process_tree
from tracefix.jobs.domain import JobError


class KernelCall:
    def __init__(self, result=1, callback=None):
        self.result, self.callback, self.calls = result, callback, []

    def __call__(self, *args):
        self.calls.append(args)
        return self.callback(*args) if self.callback else self.result


@pytest.fixture
def kernel(monkeypatch):
    functions = {name: KernelCall() for name in (
        "CreateJobObjectW", "SetInformationJobObject", "AssignProcessToJobObject",
        "OpenProcess", "CloseHandle", "TerminateJobObject", "QueryInformationJobObject")}
    functions["CreateJobObjectW"].result = 100
    functions["OpenProcess"].result = 200
    api = SimpleNamespace(**functions)
    monkeypatch.setattr(process_tree, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: api, raising=False)
    return api


@pytest.mark.parametrize("fault", ["create", "configure"])
def test_job_creation_failure_never_exposes_a_usable_scope_or_leaks_a_handle(kernel, fault):
    if fault == "create":
        kernel.CreateJobObjectW.result = 0
    else:
        kernel.SetInformationJobObject.result = 0
    with pytest.raises(JobError, match="scope_unavailable"):
        process_tree.WindowsJobScope()
    assert kernel.CloseHandle.calls == ([] if fault == "create" else [(100,)])
    assert not kernel.OpenProcess.calls and not kernel.AssignProcessToJobObject.calls


@pytest.mark.parametrize("fault", ["open", "assign"])
def test_assignment_failure_closes_process_handle_and_does_not_authorize_execution(kernel, fault):
    scope = process_tree.WindowsJobScope()
    if fault == "open":
        kernel.OpenProcess.result = 0
    else:
        kernel.AssignProcessToJobObject.result = 0
    with pytest.raises(JobError, match="assignment_failed"):
        scope._attach(SimpleNamespace(pid=42))
    assert kernel.CloseHandle.calls == ([] if fault == "open" else [(200,)])
    assert scope.process_started is False
    assert scope.close() and scope.close()
    assert kernel.CloseHandle.calls[-1] == (100,)


def test_job_accounting_query_failure_is_unknown_even_when_termination_was_accepted(kernel):
    scope = process_tree.WindowsJobScope()
    kernel.QueryInformationJobObject.result = 0
    assert not scope.wait_empty(timeout=0)
    assert not scope.close() and not scope.close()
    assert kernel.TerminateJobObject.calls == [(100, 1)]
    assert kernel.CloseHandle.calls == [(100,)]
    assert not scope.wait_empty(timeout=0)


def test_job_termination_denied_keeps_failure_after_handle_is_closed(kernel):
    scope = process_tree.WindowsJobScope()
    kernel.TerminateJobObject.result = 0
    with pytest.raises(JobError, match="termination_failed"):
        scope.terminate_tree()
    assert not scope.close() and not scope.close()
    assert kernel.CloseHandle.calls == [(100,)]
    assert not kernel.QueryInformationJobObject.calls


def test_nonempty_job_cannot_be_reported_cancelled_until_accounting_is_empty(kernel, monkeypatch):
    scope = process_tree.WindowsJobScope()

    def populated(handle, info, data, length, returned):
        ctypes.memmove(data, b"\x00" * 40 + (1).to_bytes(4, "little") + b"\x00" * 4, 48)
        return 1

    kernel.QueryInformationJobObject.callback = populated
    assert not scope.wait_empty(timeout=0)
    polls = []

    def exits(seconds):
        polls.append(seconds)
        kernel.QueryInformationJobObject.callback = None

    monkeypatch.setattr(process_tree.time, "sleep", exits)
    assert scope.wait_empty(timeout=1) and polls == [0.01]
    assert scope.close() and scope.wait_empty(timeout=0)
    calls = len(kernel.TerminateJobObject.calls)
    scope.terminate_tree()
    assert len(kernel.TerminateJobObject.calls) == calls
