"""Owned process scopes. A process group alone is never proof of complete cleanup."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import signal
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4

from tracefix.jobs.domain import JobError

_PREFIX = b"TRACEFIX_JOB_CONTROL:"


class ProcessStartError(JobError):
    def __init__(self, reason: str, *, process_started: bool | None, cleaned: bool) -> None:
        super().__init__(reason)
        self.process_started = process_started
        self.tree_exit_confirmed = cleaned


class ProcessTreeScope(ABC):
    """One launch, one owner; only trusted runtime adapters construct a scope."""

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.process_started: bool | None = False
        self._closed = False
        self._cleaned = False

    @abstractmethod
    def _attach(self, process: subprocess.Popen) -> None:
        raise NotImplementedError

    def start(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str],
        startup_timeout: float = 5.0,
    ) -> subprocess.Popen:
        if self.process is not None or self._closed:
            raise JobError("process_scope_already_dispatched")
        if not argv or startup_timeout <= 0:
            raise JobError("process_scope_invalid_launch")
        nonce = uuid4().hex
        gate = Path(__file__).with_name("_bootstrap.py").resolve(strict=True)
        gate_released = False
        try:
            self.process = subprocess.Popen(
                [argv[0], "-I", str(gate)], cwd=gate.parent, env=dict(env),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            self._attach(self.process)
            request = json.dumps({"nonce": nonce, "argv": list(argv), "cwd": str(cwd)})
            if len(request.encode("utf-8")) >= 64 * 1024 - 1:
                raise JobError("process_scope_launch_too_large")
            assert self.process.stdin is not None and self.process.stdout is not None
            gate_released = True
            self.process.stdin.write(request.encode("utf-8") + b"\n")
            self.process.stdin.close()
            self.process.stdin = None
            received: queue.Queue[bytes | BaseException] = queue.Queue(maxsize=1)

            def read_header() -> None:
                try:
                    received.put(self.process.stdout.readline(4097))
                except BaseException as exc:
                    received.put(exc)

            reader = threading.Thread(target=read_header, daemon=True)
            reader.start()
            line = received.get(timeout=startup_timeout)
            if isinstance(line, BaseException):
                raise line
            if len(line) > 4096 or not line.startswith(_PREFIX):
                raise JobError("process_scope_start_ack_invalid")
            response = json.loads(line[len(_PREFIX):].decode("utf-8"))
            if response.get("nonce") != nonce or type(response.get("started")) is not bool:
                raise JobError("process_scope_start_ack_invalid")
            if response["started"] and (
                type(response.get("pid")) is not int or response["pid"] <= 0
                or set(response) != {"nonce", "started", "pid"}
            ):
                raise JobError("process_scope_start_ack_invalid")
            self.process_started = response["started"]
            if not self.process_started:
                raise JobError("process_scope_child_failed_to_start")
            return self.process
        except BaseException as exc:
            if gate_released and self.process_started is False:
                # A positively received failure ack proves no child started; other failures do not.
                if not (isinstance(exc, JobError)
                        and exc.reason_code == "process_scope_child_failed_to_start"):
                    self.process_started = None
            cleaned = self.close()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ProcessStartError(
                exc.reason_code if isinstance(exc, JobError) else "process_scope_start_failed",
                process_started=self.process_started, cleaned=cleaned,
            ) from exc

    @abstractmethod
    def terminate_tree(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_empty(self, timeout: float = 3.0) -> bool:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> bool:
        raise NotImplementedError

    def _reap_gate(self) -> None:
        if self.process is None:
            return
        try:
            self.process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2.0)


class WindowsJobScope(ProcessTreeScope):
    """Job Object membership is established before the trusted launch gate opens."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise JobError("process_tree_scope_unavailable")
        super().__init__()
        from ctypes import wintypes as w

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                ("active_limit", w.DWORD), ("affinity", ctypes.c_size_t),
                ("priority", w.DWORD), ("scheduling", w.DWORD),
            ]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimit), ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
            ]

        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            w.DWORD, ctypes.c_void_p], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self._kernel, name)
            function.argtypes, function.restype = args, result
        self._job = self._kernel.CreateJobObjectW(None, None)
        if not self._job:
            raise JobError("process_tree_scope_unavailable")
        limits = ExtendedLimit()
        limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; neither breakaway option is enabled.
        if not self._kernel.SetInformationJobObject(
            self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            self._kernel.CloseHandle(self._job)
            raise JobError("process_tree_scope_unavailable")

    def _attach(self, process: subprocess.Popen) -> None:
        handle = self._kernel.OpenProcess(0x0100 | 0x0001, False, process.pid)
        if not handle:
            raise JobError("process_scope_assignment_failed")
        try:
            if not self._kernel.AssignProcessToJobObject(self._job, handle):
                raise JobError("process_scope_assignment_failed")
        finally:
            self._kernel.CloseHandle(handle)

    def terminate_tree(self) -> None:
        if not self._closed and not self._kernel.TerminateJobObject(self._job, 1):
            raise JobError("process_scope_termination_failed")

    def wait_empty(self, timeout: float = 3.0) -> bool:
        if self._closed:
            return self._cleaned
        until = time.monotonic() + max(0.0, timeout)
        while True:
            # BASIC_ACCOUNTING_INFORMATION: four LARGE_INTEGERs followed by four DWORDs.
            data = ctypes.create_string_buffer(48)
            if not self._kernel.QueryInformationJobObject(self._job, 1, data, 48, None):
                return False
            if int.from_bytes(data.raw[40:44], "little") == 0:
                return True
            if time.monotonic() >= until:
                return False
            time.sleep(0.01)

    def close(self) -> bool:
        if self._closed:
            return self._cleaned
        try:
            self.terminate_tree()
            # Assignment can fail before the gate joins the Job; it still must be reaped.
            if self.process is not None and self.process.poll() is None:
                self.process.kill()
            self._reap_gate()
            self._cleaned = self.wait_empty()
        except (JobError, OSError, subprocess.TimeoutExpired):
            self._cleaned = False
        finally:
            self._kernel.CloseHandle(self._job)
            self._closed = True
        return self._cleaned


class LinuxCgroupScope(ProcessTreeScope):
    """Use an explicitly delegated cgroup v2 leaf; never configure host delegation."""

    def __init__(self, delegated_root: Path | None) -> None:
        super().__init__()
        if os.name == "nt" or delegated_root is None:
            raise JobError("process_tree_scope_unavailable")
        root = delegated_root.absolute()
        if not root.is_relative_to(Path("/sys/fs/cgroup")) or any(
            path.is_symlink() for path in (root, *root.parents)
        ) or not (root / "cgroup.controllers").is_file():
            raise JobError("process_tree_scope_unavailable")
        self._leaf = root / ("tracefix-job-" + uuid4().hex)
        try:
            self._leaf.mkdir()
            if not (self._leaf / "cgroup.kill").is_file():
                self._leaf.rmdir()
                raise JobError("process_tree_scope_unavailable")
        except OSError as exc:
            raise JobError("process_tree_scope_unavailable") from exc

    def _attach(self, process: subprocess.Popen) -> None:
        (self._leaf / "cgroup.procs").write_text(str(process.pid), encoding="ascii")

    def terminate_tree(self) -> None:
        if not self._closed:
            (self._leaf / "cgroup.kill").write_text("1", encoding="ascii")

    def wait_empty(self, timeout: float = 3.0) -> bool:
        if self._closed:
            return self._cleaned
        until = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fields = dict(line.split() for line in
                              (self._leaf / "cgroup.events").read_text("ascii").splitlines())
            except (OSError, ValueError):
                return False
            if fields.get("populated") == "0":
                return True
            if time.monotonic() >= until:
                return False
            time.sleep(0.01)

    def close(self) -> bool:
        if self._closed:
            return self._cleaned
        try:
            self.terminate_tree()
            self._reap_gate()
            self._cleaned = self.wait_empty()
            if self._cleaned:
                self._leaf.rmdir()
        except (OSError, subprocess.TimeoutExpired):
            self._cleaned = False
        self._closed = True
        return self._cleaned


def _process_tokens() -> set[tuple[int, str]]:
    """Kernel start times distinguish recycled PIDs; command text is never trusted."""
    result = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            # Zombies no longer execute or hold pipes; their owner must still reap them.
            if fields[0] != "Z":
                result.add((int(entry.name), fields[19]))
        except FileNotFoundError:
            continue
        except (OSError, IndexError) as exc:
            raise JobError("process_scope_inventory_unavailable") from exc
    return result


class DockerProcessScope(ProcessTreeScope):
    """One test in an owned PID namespace; unaccounted processes require executor abort."""

    def __init__(self, trusted_control_tokens: frozenset[tuple[int, str]]) -> None:
        super().__init__()
        if os.name == "nt" or not trusted_control_tokens:
            raise JobError("process_tree_scope_unavailable")
        self._trusted = trusted_control_tokens
        if _process_tokens() - self._trusted:
            raise JobError("process_scope_has_unowned_processes")

    def _attach(self, process: subprocess.Popen) -> None:
        # start_new_session created a process group before the gate can read its request.
        if os.getpgid(process.pid) != process.pid:
            raise JobError("process_scope_assignment_failed")

    def terminate_tree(self) -> None:
        if self.process is not None and not self._closed:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def wait_empty(self, timeout: float = 3.0) -> bool:
        if self._closed:
            return self._cleaned
        until = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                remaining = _process_tokens() - self._trusted
            except JobError:
                return False
            if not remaining:
                return True
            if time.monotonic() >= until:
                return False
            time.sleep(0.01)

    def close(self) -> bool:
        if self._closed:
            return self._cleaned
        try:
            self.terminate_tree()
            self._reap_gate()
            self._cleaned = self.wait_empty()
        except (OSError, subprocess.TimeoutExpired):
            self._cleaned = False
        self._closed = True
        return self._cleaned
