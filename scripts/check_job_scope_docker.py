"""Zero-provider process-domain probe; run only as the sole command in an owned container."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tracefix.artifacts import ArtifactStore
from tracefix.jobs.capture import LiveProcessCapture
from tracefix.jobs.process_tree import DockerProcessScope
from tracefix.security import minimal_subprocess_environment


def main() -> int:
    # Trust only this control process and its actual init ancestry, never a namespace census.
    trusted: set[tuple[int, str]] = set()
    pid = os.getpid()
    while pid:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        trusted.add((pid, fields[19]))
        if pid == 1:
            break
        pid = int(fields[1])
    scope = DockerProcessScope(frozenset(trusted))
    if sys.argv[1:] == ["capture"]:
        store = ArtifactStore("/tmp/evidence", run_id="docker-probe", streaming_enabled=True)
        capture = LiveProcessCapture(store, call_id="capture", source_identity={})
        cancel = threading.Event()
        command = ("import os,time; os.write(1,b'line\\n'*20000+b'MIDDLE\\n'); "
                   "os.write(2,b'password=secret-value\\n'); time.sleep(60)")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                capture.run, [sys.executable, "-c", command], cwd=Path("/tmp"),
                env=minimal_subprocess_environment(), scope=scope, cancel=cancel,
                deadline=time.monotonic() + 10,
            )
            observed, offset = "", 0
            try:
                for _ in range(40):
                    page = capture.read(offset=offset)
                    observed += page["content"]
                    offset = page["next_offset"]
                    if "MIDDLE" in observed:
                        break
                    time.sleep(0.03)
                assert "MIDDLE" in observed and not future.done()
            finally:
                cancel.set()
            result = future.result(timeout=10)
        assert result.tree_exit_confirmed and result.cancelled and result.artifact is not None
        assert result.artifact.raw_bytes > 100000
        assert "secret-value" not in store.read(result.artifact.artifact_id,
                                                stream="stderr").content
        print(json.dumps({"incremental_middle": True, "cancelled": True,
                          "cleanup_confirmed": True, "raw_bytes": result.artifact.raw_bytes,
                          "storage_bytes": store.quota().storage_bytes}), flush=True)
        return 0
    escape = sys.argv[1:] == ["escape"]
    child = "import time; time.sleep(60)"
    command = (
        "import subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session={escape!r}); "
        "print(p.pid,flush=True); time.sleep(60)"
    )
    process = scope.start([sys.executable, "-c", command], cwd=Path("/tmp"),
                          env=minimal_subprocess_environment())
    descendant = int(process.stdout.readline())
    scope.terminate_tree()
    cleaned = scope.wait_empty(timeout=1)
    if escape:
        assert not cleaned, "escaped descendant must force whole-container abort"
        assert not scope.close()
        print(json.dumps({"escaped_pid": descendant, "cleanup_confirmed": False,
                          "requires_container_abort": True}), flush=True)
        # The owner must observe a failed invocation and remove its entire container.
        return 23
    assert cleaned
    process.communicate(timeout=3)
    assert scope.close()
    print(json.dumps({"cleanup_confirmed": True, "requires_container_abort": False}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
