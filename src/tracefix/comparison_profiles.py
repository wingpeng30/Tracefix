"""Versioned evaluation resources; never alter product defaults or old protocols."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from tracefix.models.input_bounds import InputBound

LEGACY_PROFILE = "abc-60k-v1"
TOKENIZER_PROFILE = "bc-350k-tokenizer"
CAPABILITY_PROFILE = "bc-capability-tokenizer"
HOLDOUT_PROFILE = "abc-holdout-capability-v1"
BC_HOLDOUT_PROFILE = "bc-holdout-180s-v1"
AC_HOLDOUT_PROFILE = "ac-30-180s-v1"
HOLDOUT_PROFILES = (HOLDOUT_PROFILE, BC_HOLDOUT_PROFILE, AC_HOLDOUT_PROFILE)


def profile_config(name: str = LEGACY_PROFILE) -> dict:
    if name in HOLDOUT_PROFILES:
        selected = {
            "name": name,
            "arms": ["A", "C"]
            if name == AC_HOLDOUT_PROFILE
            else (["B", "C"] if name == BC_HOLDOUT_PROFILE else ["A", "B", "C"]),
            "repetitions": 1 if name == AC_HOLDOUT_PROFILE else 3,
            "limits": {
                "input_tokens": 9223372036854775807,
                "output_tokens": 9223372036854775807,
                "requests": 2147483647,
                "tests": 2147483647,
                "active_seconds": 3600,
                "timeout_seconds": 300 if name == HOLDOUT_PROFILE else 180,
                "limit_cny": 300.0,
            },
            "per_request_output_tokens": 393216,
            "arm_limit_cny": None,
            "trial_limit_cny": 10.0,
            "input_counter": "official_estimate",
            "capability_mode": True,
        }
        if name != HOLDOUT_PROFILE:
            selected["hard_request_deadline_seconds"] = 180
        if name == AC_HOLDOUT_PROFILE:
            selected.update(task_count=30, funding_mode="new_authorization")
        return selected
    if name not in {LEGACY_PROFILE, TOKENIZER_PROFILE, CAPABILITY_PROFILE}:
        raise ValueError("unknown comparison profile")
    new = name != LEGACY_PROFILE
    capability = name == CAPABILITY_PROFILE
    return {
        "name": name,
        "arms": ["B", "C"] if new else ["A", "B", "C"],
        "repetitions": 1 if new else 3,
        "limits": {
            "input_tokens": 1250000 if capability else (350000 if new else 60000),
            "output_tokens": 312500 if capability else (60000 if new else 8000),
            "requests": 2147483647 if capability else (100 if new else 20),
            "tests": 2147483647 if capability else (64 if new else 16),
            "active_seconds": 2147483647 if capability else (3600 if new else 900),
            "timeout_seconds": 120 if new else 60,
            "limit_cny": 50.0 if new else 20.0,
        },
        "per_request_output_tokens": 393216 if capability else (8192 if new else 2048),
        "arm_limit_cny": 25.0 if new else None,
        "trial_limit_cny": 2.5 if new else None,
        "input_counter": "official_estimate" if new else "legacy_utf8_bound",
        "capability_mode": capability,
    }


def protocol_profile(protocol: dict) -> dict:
    selected = protocol.get("profile", profile_config())
    if selected != profile_config(selected["name"]):
        raise ValueError("frozen profile changed")
    return selected


class OfficialCounter:
    """Reuse one identity-pinned worker per run; never substitute a tokenizer."""

    def __init__(self, runtime: dict, *, timeout_seconds: float = 120.0):
        self.runtime = runtime
        self._timeout_seconds = timeout_seconds
        # Keep construction lazy for offline callers that replace ``invoke``
        # (for example calibration tests); real invocation still requires a
        # command and will raise the normal KeyError at that boundary.
        command = runtime.get("command", [])
        self._persistent = runtime.get("mode") == "jsonl-v1" or (
            "-m" in command
            and command[command.index("-m") + 1:command.index("-m") + 2]
            == ["tracefix.comparison_counter"]
        )
        self._process: subprocess.Popen | None = None
        self._stdout: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._stderr_tail = bytearray()
        self._stderr_lock = threading.Lock()
        self._request_id = 0
        self._identity_digest: str | None = None
        self._closed = False
        self._failed = False
        self._lock = threading.Lock()

    @staticmethod
    def _read_stdout(stream, target: queue.Queue) -> None:
        try:
            while True:
                line = stream.readline(8 * 1024 * 1024 + 1)
                if not line:
                    target.put(("eof", None))
                    return
                if len(line) > 8 * 1024 * 1024 or not line.endswith(b"\n"):
                    target.put(("error", "worker frame exceeds limit or is incomplete"))
                    return
                target.put(("line", line))
        except Exception as exc:
            target.put(("error", f"stdout reader failed: {type(exc).__name__}"))

    def _read_stderr(self, stream) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                with self._stderr_lock:
                    self._stderr_tail.extend(chunk)
                    if len(self._stderr_tail) > 64 * 1024:
                        del self._stderr_tail[:-64 * 1024]
        except Exception:
            return

    def _start(self, timeout: float) -> None:
        if self._closed or self._failed:
            raise ValueError("official counter worker is closed or failed")
        if self._process is not None:
            return
        command = list(self.runtime["command"])
        if command and command[-1] != "--serve":
            command.append("--serve")
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,
            env={**os.environ, "PYTHONIOENCODING": "utf-8:strict"},
        )
        self._process = process
        threading.Thread(
            target=self._read_stdout, args=(process.stdout, self._stdout), daemon=True
        ).start()
        threading.Thread(target=self._read_stderr, args=(process.stderr,), daemon=True).start()
        frame = self._read_frame(timeout)
        if frame.get("protocol") != 1 or frame.get("ready") is not True:
            raise ValueError("official counter sent an invalid startup frame")
        identity = frame.get("identity")
        identity_digest = frame.get("identity_sha256")
        actual_digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        if not isinstance(identity, dict) or identity_digest != actual_digest:
            raise ValueError("official counter startup identity is invalid")
        expected = self.runtime.get("identity")
        if expected is not None and identity != expected:
            raise ValueError("official counter runtime identity changed")
        self._identity_digest = actual_digest

    def _read_frame(self, timeout: float) -> dict:
        try:
            kind, value = self._stdout.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("official counter worker timed out") from exc
        if kind != "line":
            raise ValueError(f"official counter worker {kind}: {value or self._stderr_text()}")
        try:
            frame = json.loads(value.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("official counter worker returned invalid JSON") from exc
        if not isinstance(frame, dict):
            raise ValueError("official counter worker frame must be an object")
        return frame

    def _stderr_text(self) -> str:
        with self._stderr_lock:
            return self._stderr_tail.decode("utf-8", errors="replace")[-2000:]

    def _persistent_invoke(self, bodies: list[dict] | None, timeout: float) -> dict:
        with self._lock:
            try:
                deadline = time.monotonic() + timeout
                self._start(max(0.001, deadline - time.monotonic()))
                assert self._process is not None and self._process.stdin is not None
                self._request_id += 1
                request_id = self._request_id
                payload = json.dumps(
                    {"protocol": 1, "id": request_id, "bodies": bodies or []},
                    ensure_ascii=False, separators=(",", ":"),
                )
                write_result: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
                threading.Thread(
                    target=self._write_frame,
                    args=(self._process.stdin, (payload + "\n").encode("utf-8"), write_result),
                    daemon=True,
                ).start()
                try:
                    write_kind, write_error = write_result.get(
                        timeout=max(0.001, deadline - time.monotonic())
                    )
                except queue.Empty as exc:
                    raise TimeoutError("official counter worker timed out while writing") from exc
                if write_kind != "ok":
                    raise ValueError(f"official counter worker write failed: {write_error}")
                frame = self._read_frame(max(0.001, deadline - time.monotonic()))
                if frame.get("protocol") != 1 or frame.get("id") != request_id:
                    raise ValueError("official counter response ID or protocol changed")
                if frame.get("identity_sha256") != self._identity_digest:
                    raise ValueError("official counter identity changed during request")
                if frame.get("ok") is not True:
                    raise ValueError("official counter rejected request")
                counts = frame.get("counts")
                if not isinstance(counts, list) or len(counts) != len(bodies or []):
                    raise ValueError("official counter returned the wrong result count")
                return {"identity": self.runtime.get("identity"), "counts": counts}
            except Exception:
                self._failed = True
                self._stop_process(grace_seconds=0.2)
                raise

    @staticmethod
    def _write_frame(stream, payload: bytes, result: queue.Queue) -> None:
        try:
            stream.write(payload)
            stream.flush()
            result.put(("ok", None))
        except Exception as exc:
            result.put(("error", type(exc).__name__))

    def _stop_process(self, *, grace_seconds: float) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=grace_seconds)
        except Exception:
            try:
                process.terminate()
                process.wait(timeout=1)
            except Exception:
                process.kill()
                try:
                    process.wait(timeout=1)
                except Exception:
                    pass
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop_process(grace_seconds=2)

    def invoke(self, bodies: list[dict] | None = None) -> dict:
        result = subprocess.run(
            self.runtime["command"],
            input=json.dumps({} if bodies is None else {"bodies": bodies}, ensure_ascii=False),
            capture_output=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8:strict"},
            timeout=1800 if bodies and len(bodies) > 1 else 120,
            check=True,
        )
        parsed = json.loads(result.stdout)
        if "identity" in self.runtime and parsed["identity"] != self.runtime["identity"]:
            raise ValueError("official counter runtime identity changed")
        return parsed

    def count(self, kwargs: dict) -> InputBound:
        return self.count_many([kwargs])[0]

    def count_many(self, requests: list[dict]) -> list[InputBound]:
        """Count a batch under the existing single/batch deadlines."""
        if not requests:
            return []
        bodies = [
            {
                "model": kwargs["model"].split("/")[-1],
                "messages": kwargs["messages"],
                "tools": kwargs.get("tools", []),
                "thinking": kwargs.get("extra_body", {}).get("thinking"),
            }
            for kwargs in requests
        ]
        if self._persistent:
            timeout = self._timeout_seconds if len(bodies) == 1 else 1800.0
            counts = self._persistent_invoke(bodies, timeout)["counts"]
        else:
            counts = self.invoke(bodies)["counts"]
        return [InputBound(**item) for item in counts]


def calibrate_previous(previous: Path, runtime: dict) -> dict:
    """Replay exactly the previous 212 paid requests without invoking a provider."""
    import hashlib

    ledger = json.loads((previous / "requests.json").read_text(encoding="utf-8"))
    if len(ledger["requests"]) != 212:
        raise ValueError("exact previous 212 requests required")
    bodies, hashes, expected = [], {}, []
    for row in ledger["requests"]:
        if row["status"] != "completed":
            raise ValueError("previous request is not completed")
        path = previous / "provider" / (row["id"] + "-request.json")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if sha != row["request_sha256"]:
            raise ValueError("previous request corrupted")
        response = previous / "provider" / (row["id"] + "-response.json")
        response_sha = hashlib.sha256(response.read_bytes()).hexdigest()
        if response_sha != row["response_sha256"]:
            raise ValueError("previous response corrupted")
        raw = json.loads(response.read_text(encoding="utf-8"))
        if raw["usage"] != row["usage"]:
            raise ValueError("previous usage changed")
        kwargs = json.loads(path.read_text(encoding="utf-8"))
        bodies.append(
            {
                "model": kwargs["model"].split("/")[-1],
                "messages": kwargs["messages"],
                "tools": kwargs.get("tools", []),
                "thinking": kwargs.get("extra_body", {}).get("thinking"),
            }
        )
        hashes[row["id"]] = {"request": sha, "response": response_sha}
        expected.append(row["usage"]["input_tokens"])
    result = OfficialCounter(runtime).invoke(bodies)
    rows = [
        {
            "id": row["id"],
            "count": count,
            "actual": actual,
            "delta": None if count["tokens"] is None else count["tokens"] - actual,
        }
        for row, count, actual in zip(ledger["requests"], result["counts"], expected, strict=True)
    ]
    return {
        "accepted": len(rows) == 212
        and all(r["count"]["status"] == "estimate" and r["delta"] == 0 for r in rows),
        "supplier_calls": 0,
        "ledger_sha256": hashlib.sha256((previous / "requests.json").read_bytes()).hexdigest(),
        "previous": str(previous.resolve()),
        "hashes": hashes,
        "runtime": {**runtime, "identity": result["identity"]},
        "rows": rows,
    }
