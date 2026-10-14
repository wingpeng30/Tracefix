"""Synchronous bounded subprocess capture used by artifact-enabled tools."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from tracefix.artifacts import ArtifactError, ArtifactRef, ArtifactStore


@dataclass(frozen=True)
class ProcessCaptureResult:
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool
    incomplete: bool
    artifact: ArtifactRef | None
    artifact_error: str | None
    reader_error: str | None


class _Budget:
    def __init__(self, maximum: int) -> None:
        self.remaining = maximum
        self.lock = threading.Lock()
        self.exhausted = False
        self.write_failed = False

    def fail(self) -> None:
        with self.lock:
            self.write_failed = True
            self.exhausted = True
            self.remaining = 0

    def take(self, data: bytes, output: object) -> None:
        with self.lock:
            if self.write_failed:
                return
            accepted = min(self.remaining, len(data))
            if accepted:
                try:
                    written = output.write(data[:accepted])  # type: ignore[attr-defined]
                except OSError:
                    self.write_failed = True
                    self.exhausted = True
                    self.remaining = 0
                    return
                if written != accepted:
                    self.write_failed = True
                    self.exhausted = True
                    self.remaining = 0
                    return
                self.remaining -= accepted
            if accepted < len(data):
                self.exhausted = True


class _Preview:
    """Retain bounded head/tail bytes without buffering the process output."""

    _MAX = 64 * 1024
    _HALF = _MAX // 2

    def __init__(self) -> None:
        self.total = 0
        self._small = bytearray()
        self._head = bytearray()
        self._tail = bytearray()
        self._large = False

    def add(self, data: bytes) -> None:
        self.total += len(data)
        if not self._large and self.total <= self._MAX:
            self._small.extend(data)
            return
        if not self._large:
            self._large = True
            initial = bytes(self._small) + data
            self._head.extend(initial[: self._HALF])
            self._tail.extend(initial[self._HALF :][-self._HALF :])
            self._small.clear()
            return
        self._tail.extend(data)
        if len(self._tail) > self._HALF:
            del self._tail[: len(self._tail) - self._HALF]

    def text(self) -> str:
        raw = bytes(self._head + self._tail) if self._large else bytes(self._small)
        return raw.decode("utf-8", errors="replace")


class ProcessCapture:
    """Drain both output pipes, spool within an artifact budget, and always reap."""

    def __init__(self, store: ArtifactStore, *, drain_timeout_seconds: float = 3.0) -> None:
        if drain_timeout_seconds <= 0:
            raise ValueError("drain timeout must be positive")
        self.store = store
        self.drain_timeout_seconds = drain_timeout_seconds

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        timeout: float,
        call_id: str,
        tool_name: str,
        source_identity: dict[str, str],
        on_started: Callable[[], None] | None = None,
    ) -> ProcessCaptureResult:
        if timeout <= 0:
            raise ValueError("process timeout must be positive")
        budget = _Budget(self.store.capture_budget())
        with ExitStack() as stack:
            spools = {}
            errors: list[str] = []
            for name in ("stdout", "stderr"):
                try:
                    spools[name] = stack.enter_context(tempfile.TemporaryFile(mode="w+b"))
                except OSError:
                    spools[name] = None
                    budget.fail()
                    errors.append("storage_open_failed")
            previews = {name: _Preview() for name in spools}
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=dict(env),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                shell=False,
                start_new_session=(os.name != "nt"),
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
            readers: list[threading.Thread] = []

            def drain(name: str, pipe) -> None:
                output = spools[name]
                try:
                    while True:
                        chunk = pipe.read(64 * 1024)
                        if not chunk:
                            break
                        previews[name].add(chunk)
                        if output is not None:
                            budget.take(chunk, output)
                    if output is not None:
                        output.flush()
                        os.fsync(output.fileno())
                except OSError:
                    budget.fail()
                    errors.append("storage_write_failed")
                except BaseException as exc:
                    errors.append(type(exc).__name__)

            for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                assert pipe is not None
                thread = threading.Thread(target=drain, args=(name, pipe), daemon=True)
                thread.start()
                readers.append(thread)

            timed_out = False
            callback_error: BaseException | None = None
            try:
                if on_started is not None:
                    on_started()
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate(process)
            except BaseException as exc:
                callback_error = exc
                self._terminate(process)
            for reader in readers:
                reader.join(timeout=self.drain_timeout_seconds)
            if any(reader.is_alive() for reader in readers):
                self._terminate(process, include_descendants=True)
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            pass
                for reader in readers:
                    reader.join(timeout=0.25)
                errors.append("drain_timeout")
            if process.poll() is None:
                self._terminate(process)
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if callback_error is not None:
                raise callback_error

            raw = {}
            for name, spool in spools.items():
                if spool is None:
                    raw[name] = b""
                    continue
                spool.seek(0)
                raw[name] = spool.read()
            incomplete = timed_out or budget.exhausted or bool(errors)
            artifact: ArtifactRef | None = None
            artifact_error: str | None = (
                "capture_storage_write_failed" if budget.write_failed else None
            )
            try:
                artifact = self.store.publish_process(
                    call_id=call_id,
                    tool_name=tool_name,
                    source_identity=source_identity,
                    stdout=raw["stdout"],
                    stderr=raw["stderr"],
                    complete=not incomplete,
                )
            except (ArtifactError, OSError) as exc:
                incomplete = True
                artifact_error = (
                    "artifact_quota_exhausted"
                    if "quota" in str(exc).casefold() or "limit" in str(exc).casefold()
                    else f"artifact_publish_failed:{type(exc).__name__}"
                )
            return ProcessCaptureResult(
                stdout=previews["stdout"].text(),
                stderr=previews["stderr"].text(),
                returncode=process.returncode,
                timed_out=timed_out,
                incomplete=incomplete,
                artifact=artifact,
                artifact_error=artifact_error,
                reader_error=",".join(errors) if errors else None,
            )

    @staticmethod
    def _terminate(process: subprocess.Popen, *, include_descendants: bool = False) -> None:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None or include_descendants:
            if include_descendants:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        timeout=5.0,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
