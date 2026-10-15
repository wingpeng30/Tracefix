"""Session-owned dual-stream capture; workers never write the bridge protocol channel."""

from __future__ import annotations

import math
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tracefix.artifacts import ArtifactError, ArtifactRef, ArtifactStore
from tracefix.jobs.process_tree import ProcessStartError, ProcessTreeScope
from tracefix.jobs.redaction import StreamingRedactor


@dataclass(frozen=True, slots=True)
class LiveCaptureResult:
    stdout: str
    stderr: str
    returncode: int | None
    process_started: bool | None
    tree_exit_confirmed: bool
    cancelled: bool
    deadline_exceeded: bool
    incomplete: bool
    artifact: ArtifactRef | None
    storage_error: str | None
    process_error: str | None


class LiveProcessCapture:
    """Drain after quota/storage failure, while exposing only committed safe output."""

    def __init__(self, store: ArtifactStore, *, call_id: str,
                 source_identity: dict[str, str]) -> None:
        self.store = store
        self.call_id = call_id
        self.source_identity = dict(source_identity)
        self._redactors = {name: StreamingRedactor(secret_values=store.secret_values)
                           for name in ("stdout", "stderr")}
        self.reservation = store.reserve_capture()
        self._lock = threading.RLock()
        self._drop_writes = False
        self._started = False
        self._artifact: ArtifactRef | None = None
        self._previews = {"stdout": "", "stderr": ""}
        self._incomplete = False
        self._storage_error: str | None = None
        self._reader_error: str | None = None

    def read(self, *, stream: str = "stdout", offset: int = 0, limit: int = 4096) -> dict:
        with self._lock:
            if self._artifact is not None:
                page = self.store.read(self._artifact.artifact_id, stream=stream,
                                       offset=offset, limit=limit)
                return {**page.model_dump(mode="json"), "final": True}
            return self.store.read_capture(self.reservation.capture_id, stream=stream,
                                           offset=offset, limit=limit)

    def _append(self, name: str, raw: bytes, view: str) -> None:
        with self._lock:
            if self._drop_writes:
                return
            try:
                accepted = self.store.append_capture(self.reservation.capture_id,
                                                     stream=name, raw=raw, view=view)
            except (OSError, ArtifactError) as exc:
                self._drop_writes = self._incomplete = True
                self._storage_error = type(exc).__name__
                return
            self._incomplete |= not accepted
            if not accepted:
                self._drop_writes = True
            # A preview never includes bytes rejected by the committed view quota.
            if accepted:
                preview = self._previews[name] + view
                if len(preview) > 4096:
                    preview = preview[:2048] + "\n[preview clipped]\n" + preview[-2000:]
                self._previews[name] = preview

    def seal_unstarted(self, *, tree_exit_confirmed: bool, reason: str) -> LiveCaptureResult:
        """Seal a reservation only when this capture has never attempted process launch."""
        with self._lock:
            if self._started:
                raise ValueError("capture has already attempted launch")
            self._started = self._drop_writes = True
            try:
                self._artifact = self.store.publish_capture(
                    self.reservation.capture_id, call_id=self.call_id, tool_name="run_tests",
                    source_identity=self.source_identity, complete=True,
                )
            except (OSError, ArtifactError) as exc:
                self._incomplete = True
                self._storage_error = type(exc).__name__
            return LiveCaptureResult("", "", None, False, tree_exit_confirmed, False, False,
                                     self._incomplete, self._artifact, self._storage_error, reason)

    def run(self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str],
            scope: ProcessTreeScope, cancel: threading.Event, deadline: float,
            on_started: Callable[[], None] | None = None,
            drain_timeout: float = 3.0) -> LiveCaptureResult:
        if not math.isfinite(deadline) or not 0 < drain_timeout <= 10:
            raise ValueError("invalid capture deadline or drain timeout")
        with self._lock:
            if self._started:
                raise ValueError("live capture has already dispatched")
            self._started = True
        readers: list[threading.Thread] = []
        process: subprocess.Popen | None = None
        cancelled = expired = False
        process_error = None
        returncode = None

        def drain(name: str, pipe) -> None:
            redactor = self._redactors[name]
            try:
                while True:
                    chunk = pipe.read1(64 * 1024)
                    if not chunk:
                        break
                    with self._lock:
                        if self._drop_writes:
                            continue
                    self._append(name, chunk, redactor.feed(chunk))
                self._append(name, b"", redactor.finish())
                with self._lock:
                    self._incomplete |= redactor.incomplete
            except Exception as exc:
                with self._lock:
                    self._reader_error = type(exc).__name__
                    self._incomplete = True

        try:
            cancelled = cancel.is_set()
            expired = time.monotonic() >= deadline
            if not cancelled and not expired:
                process = scope.start(command, cwd=cwd, env=env,
                                      startup_timeout=min(5, deadline - time.monotonic()))
                if on_started is not None:
                    on_started()
                for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                    reader = threading.Thread(target=drain, args=(name, pipe), daemon=True,
                                              name=f"tracefix-output-{name}")
                    reader.start()
                    readers.append(reader)
                while process.poll() is None:
                    cancelled = cancel.is_set()
                    expired = time.monotonic() >= deadline
                    if cancelled or expired:
                        scope.terminate_tree()
                        break
                    cancel.wait(min(0.02, max(0, deadline - time.monotonic())))
                returncode = process.poll()
        except ProcessStartError as exc:
            process_error = exc.reason_code
        except Exception as exc:
            process_error = type(exc).__name__
        finally:
            try:
                cleaned = scope.close()
            except Exception as exc:
                cleaned = False
                process_error = process_error or type(exc).__name__
            until = time.monotonic() + drain_timeout
            for reader in readers:
                reader.join(max(0, until - time.monotonic()))
            with self._lock:
                self._drop_writes = True
                self._incomplete |= process_error is not None
                if any(reader.is_alive() for reader in readers) or not cleaned:
                    self._incomplete = True
                if self._reader_error:
                    process_error = process_error or self._reader_error
                # No reader can append after this point, including after a failed drain.
                try:
                    self._artifact = self.store.publish_capture(
                        self.reservation.capture_id, call_id=self.call_id, tool_name="run_tests",
                        source_identity=self.source_identity, complete=not self._incomplete,
                    )
                except (OSError, ArtifactError) as exc:
                    self._incomplete = True
                    self._storage_error = self._storage_error or type(exc).__name__
            owned_process = process or scope.process
            if owned_process is not None and not any(reader.is_alive() for reader in readers):
                for pipe in (owned_process.stdout, owned_process.stderr):
                    if pipe is not None:
                        pipe.close()
            if process is not None:
                returncode = process.returncode
        return LiveCaptureResult(
            self._previews["stdout"], self._previews["stderr"], returncode,
            scope.process_started, cleaned, cancelled, expired, self._incomplete,
            self._artifact, self._storage_error, process_error,
        )
