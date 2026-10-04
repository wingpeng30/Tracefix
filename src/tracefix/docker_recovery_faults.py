"""Real Linux container fault injection for bounded, fail-closed recovery."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from tracefix import docker_backend
from tracefix.docker_backend import DockerToolBackend
from tracefix.docker_recovery import validate_batch
from tracefix.exceptions import WorkspaceError
from tracefix.memory import atomic_json
from tracefix.memory_replay import _PATCH


def run_snapshot_faults(output: Path, source: Path, image_id: str) -> dict:
    output.mkdir(exist_ok=False)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    backend = DockerToolBackend(task_id="tracefix-ordinary", input_root=output, run_dir=output,
                                run_id=output.name, profile="ordinary", image_id=image_id)
    faults = []

    def execute(code: str) -> None:
        subprocess.run(["docker", "exec", backend.container_name, "python", "-c", code],
                       check=True, timeout=30)

    def reject_snapshot(name: str) -> None:
        try:
            backend.save_snapshot(2)
        except WorkspaceError as exc:
            faults.append({"fault": name, "rejected": True, "reason": str(exc)})
        else:
            raise AssertionError(f"unsafe {name} snapshot accepted")

    try:
        backend.prepare(commit, source, Path(__file__).resolve().parents[2],
                        source_import_probe="widget")
        saved = backend.save_snapshot(1)
        previous_sha = hashlib.sha256((output / saved["archive"]).read_bytes()).hexdigest()
        execute("from pathlib import Path; "
                "Path('/work/agent/outside-link').symlink_to('/tmp/outside')")
        reject_snapshot("outside-link")
        execute("from pathlib import Path; Path('/work/agent/outside-link').unlink(); "
                "f=Path('/work/agent/oversize.bin').open('wb'); f.truncate(1073741825); f.close()")
        reject_snapshot("1GiB-cap")
        execute("from pathlib import Path; Path('/work/agent/oversize.bin').unlink()")
        original_replace = os.replace

        def replace(source_path, target_path):
            if Path(target_path).suffix == ".tar":
                raise OSError("injected snapshot publication failure")
            return original_replace(source_path, target_path)

        with patch("tracefix.docker_backend.os.replace", replace):
            try:
                backend.save_snapshot(2)
            except OSError as exc:
                faults.append({"fault": "snapshot-write", "rejected": True, "reason": str(exc)})
            else:
                raise AssertionError("snapshot publication failure was ignored")
        if hashlib.sha256((output / saved["archive"]).read_bytes()).hexdigest() != previous_sha:
            raise AssertionError("previous snapshot changed after failed publication")
        if list((output / "docker-checkpoints").glob("*.partial")):
            raise AssertionError("incomplete snapshot artifact survived")
        validate_batch(output, saved, sequence=1, image_id=image_id)
        faults.append({"fault": "previous-batch", "preserved": True})
        original_frame = backend.session._next_frame

        def lose_result(timeout):
            original_frame(timeout)
            raise WorkspaceError("injected disconnect after container modification")

        with patch.object(backend.session, "_next_frame", lose_result):
            try:
                backend.session.call("apply_patch", {"patch": _PATCH})
            except WorkspaceError:
                pass
            else:
                raise AssertionError("unknown modifying outcome reported success")
        execute("from pathlib import Path; "
                "assert 'return page + 1' in Path('/work/agent/widget.py').read_text()")
        try:
            validate_batch(output, saved, sequence=1, image_id=image_id)
        except Exception as exc:
            if "unknown" not in str(exc):
                raise
            faults.append({"fault": "unknown-modification", "rejected": True, "reason": str(exc)})
        else:
            raise AssertionError("unknown modifying result allowed automatic recovery")
        original_run = docker_backend._run

        def fail_removal(command, **kwargs):
            if command[1:3] == ["rm", "-f"]:
                raise WorkspaceError("injected container cleanup failure")
            return original_run(command, **kwargs)

        with patch("tracefix.docker_backend._run", fail_removal):
            try:
                backend.close(remove=True)
            except WorkspaceError as exc:
                faults.append({"fault": "cleanup-failure", "reported": True, "reason": str(exc)})
            else:
                raise AssertionError("container cleanup failure was ignored")
    finally:
        container = backend.container_id
        backend.close(remove=True)
        if container and subprocess.run(["docker", "inspect", container], capture_output=True,
                                        timeout=30, check=False).returncode == 0:
            raise AssertionError("fault-injection container survived cleanup")
    summary = {"passed": True, "provider_calls": 0, "image_id": image_id, "faults": faults,
               "container_removed": True}
    atomic_json(output / "faults.json", summary)
    return summary
