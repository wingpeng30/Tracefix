from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tracefix.artifacts import ArtifactStore
from tracefix.jobs.capture import LiveProcessCapture
from tracefix.jobs.process_tree import LinuxCgroupScope, WindowsJobScope
from tracefix.security import minimal_subprocess_environment

pytestmark = pytest.mark.skipif(
    os.name != "nt" and not os.environ.get("TRACEFIX_TEST_CGROUP_ROOT"),
    reason="native Windows Job Object or explicitly delegated Linux cgroup required",
)


def native_scope():
    if os.name == "nt":
        return WindowsJobScope()
    from pathlib import Path
    return LinuxCgroupScope(Path(os.environ["TRACEFIX_TEST_CGROUP_ROOT"]))


def test_incremental_middle_output_is_readable_before_cancel_and_final_artifact(tmp_path):
    store = ArtifactStore(tmp_path / "evidence", run_id="run1", streaming_enabled=True)
    capture = LiveProcessCapture(store, call_id="call1", source_identity={})
    cancel = threading.Event()
    code = (
        "import os,time; "
        "os.write(1,b'start\\n' + b'line\\n'*20000 + "
        f"{('MIDDLE-中文🙂' + chr(10)).encode()!r}); "
        "os.write(2,b'stderr-middle\\n'); time.sleep(60)"
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(capture.run, [sys.executable, "-c", code],
                                 cwd=tmp_path, env=minimal_subprocess_environment(),
                                 scope=native_scope(), cancel=cancel,
                                 deadline=time.monotonic() + 10)
        observed = ""
        offset = 0
        try:
            for _ in range(40):
                page = capture.read(offset=offset, limit=4096)
                observed += page["content"]
                offset = page["next_offset"]
                if "MIDDLE-中文🙂" in observed:
                    break
                time.sleep(0.03)
            assert "MIDDLE-中文🙂" in observed and not future.done()
        finally:
            cancel.set()
        result = future.result(timeout=10)
    assert result.cancelled and result.process_started is True and result.tree_exit_confirmed
    assert result.artifact is not None and result.artifact.complete
    hits = store.search("stderr-middle")
    assert len(hits.hits) == 1


def test_deadline_bad_bytes_and_dual_stream_output_are_sealed(tmp_path):
    store = ArtifactStore(tmp_path / "evidence", run_id="run1", streaming_enabled=True)
    capture = LiveProcessCapture(store, call_id="call1", source_identity={})
    code = (
        "import os,time; os.write(1,b'good\\xff\\npassword=abc\\n'); "
        "os.write(2,b'error\\n'); time.sleep(60)"
    )
    result = capture.run([sys.executable, "-c", code], cwd=tmp_path,
                         env=minimal_subprocess_environment(), scope=native_scope(),
                         cancel=threading.Event(), deadline=time.monotonic() + 0.5)
    assert result.deadline_exceeded and result.tree_exit_confirmed
    assert result.artifact is not None
    stdout = store.read(result.artifact.artifact_id).content
    assert "good�" in stdout and "abc" not in stdout
    assert store.read(result.artifact.artifact_id, stream="stderr").content == "error\n"


def test_storage_failure_keeps_draining_and_never_returns_fake_artifact(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "evidence", run_id="run1", streaming_enabled=True)
    capture = LiveProcessCapture(store, call_id="call1", source_identity={})
    monkeypatch.setattr(store, "append_capture", lambda *args, **kwargs: (_ for _ in ()).throw(
        OSError("disk full")))
    code = "import os; os.write(1,b'x'*200000); os.write(2,b'y'*200000)"
    result = capture.run([sys.executable, "-c", code], cwd=tmp_path,
                         env=minimal_subprocess_environment(), scope=native_scope(),
                         cancel=threading.Event(), deadline=time.monotonic() + 10)
    assert result.returncode == 0 and result.tree_exit_confirmed
    assert result.incomplete and result.storage_error
    assert result.artifact is None or not result.artifact.complete
