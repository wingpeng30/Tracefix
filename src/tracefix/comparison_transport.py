"""Evaluation-only process deadline, independent of HTTP keep-alive traffic."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from tracefix.exceptions import LLMProviderError


def worker_command() -> list[str]:
    # Windows venv redirectors create another process: terminate the actual
    # interpreter directly so the HTTP request cannot outlive its worker.
    executable = getattr(sys, "_base_executable", sys.executable)
    return [executable, "-m", "tracefix.comparison_transport", "--worker"]


def transport_identity() -> dict:
    command = worker_command()
    return {
        "command": command,
        "interpreter_sha256": hashlib.sha256(Path(command[0]).read_bytes()).hexdigest(),
        "pythonpath": [str(Path(__file__).resolve().parents[1])]
        + [str(Path(p).resolve()) for p in sys.path if p and "site-packages" in Path(p).parts],
    }


class HardDeadlineClient:
    """A killed/failed worker has unknown usage; the caller retains its reservation."""

    def __init__(self, seconds: float):
        if seconds <= 0:
            raise ValueError("positive hard request deadline required")
        self.seconds = seconds

    def completion(self, **kwargs):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(transport_identity()["pythonpath"])
        process = subprocess.Popen(
            worker_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        try:
            output, errors = process.communicate(json.dumps(kwargs), timeout=self.seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise LLMProviderError(
                f"hard request deadline exceeded ({self.seconds}s); "
                "response/usage unknown; no retry"
            ) from exc
        if process.returncode:
            raise LLMProviderError(
                f"provider worker exited {process.returncode}; "
                f"stderr_sha256={hashlib.sha256(errors.encode()).hexdigest()}; no retry"
            )
        try:
            result = json.loads(output)
            if not result["ok"]:
                raise LLMProviderError(f"provider worker failed: {result['error_type']}; no retry")
            return result["response"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMProviderError(
                "invalid provider worker response; usage unknown; no retry"
            ) from exc


def main() -> int:
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("internal evaluation worker only")
    try:
        import litellm

        response = litellm.completion(**json.load(sys.stdin))
        result = {"ok": True, "response": response.model_dump(mode="json")}
    except Exception as exc:
        # Keep secrets, request bodies and provider errors out of diagnostics.
        result = {"ok": False, "error_type": type(exc).__name__}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
