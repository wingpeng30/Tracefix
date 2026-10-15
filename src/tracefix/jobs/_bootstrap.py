"""Trusted launch gate. Run by absolute path with Python -I, never as workspace code."""

from __future__ import annotations

import json
import subprocess
import sys
import threading

PREFIX = b"TRACEFIX_JOB_CONTROL:"


def _control(value: dict) -> None:
    sys.stdout.buffer.write(PREFIX + json.dumps(value).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def main() -> int:
    # No child can start until the parent has assigned this gate to its execution scope.
    request = json.loads(sys.stdin.buffer.readline(64 * 1024))
    nonce = request["nonce"]
    try:
        process = subprocess.Popen(
            request["argv"], cwd=request["cwd"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
        )
    except OSError as exc:
        _control({"nonce": nonce, "started": False, "error": type(exc).__name__})
        return 125
    _control({"nonce": nonce, "started": True, "pid": process.pid})

    def forward(source, target) -> None:
        while True:
            chunk = source.read1(64 * 1024)
            if not chunk:
                break
            target.write(chunk)
            target.flush()

    threads = [
        threading.Thread(target=forward, args=(process.stdout, sys.stdout.buffer)),
        threading.Thread(target=forward, args=(process.stderr, sys.stderr.buffer)),
    ]
    for thread in threads:
        thread.start()
    code = process.wait()
    for thread in threads:
        thread.join()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
