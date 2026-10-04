"""Fault verifier counterexamples; actual container behavior is verified separately."""

from types import SimpleNamespace

import pytest

from tracefix import docker_recovery_faults as faults
from tracefix.exceptions import WorkspaceError


@pytest.mark.parametrize("violation", [None, "accept-unsafe", "hide-disconnect", "hide-cleanup"])
def test_fault_replay_requires_rejections_and_preserves_previous_file(
    tmp_path, monkeypatch, violation,
):
    output = tmp_path / "faults"
    state = {"unsafe": False, "modified": False, "removed": False}

    class Session:
        def _next_frame(self, timeout):
            return b"result"

        def call(self, name, arguments):
            assert name == "apply_patch" and "patch" in arguments
            state["modified"] = True
            if violation != "hide-disconnect":
                self._next_frame(1)

    class Backend:
        def __init__(self, **kwargs):
            self.container_name = "owned"
            self.container_id = "owned-id"
            self.session = Session()

        def prepare(self, *args, **kwargs):
            assert kwargs["source_import_probe"] == "widget"

        def save_snapshot(self, sequence):
            if state["unsafe"] and violation != "accept-unsafe":
                raise WorkspaceError("unsafe product snapshot")
            directory = output / "docker-checkpoints"
            directory.mkdir(exist_ok=True)
            partial = directory / f"{sequence}.partial"
            try:
                partial.write_bytes(b"original verified snapshot")
                faults.os.replace(partial, directory / f"{sequence}.tar")
            finally:
                partial.unlink(missing_ok=True)
            return {"archive": f"docker-checkpoints/{sequence}.tar"}

        def close(self, remove):
            assert remove
            if violation != "hide-cleanup":
                faults.docker_backend._run(["docker", "rm", "-f", self.container_id])
            state["removed"] = True

    def run(command, **kwargs):
        if command[1] == "exec":
            code = command[-1]
            if "assert 'return page + 1'" in code:
                assert state["modified"]
            elif "symlink_to" in code or "truncate" in code:
                state["unsafe"] = True
            else:
                state["unsafe"] = False
        elif command[1] == "inspect":
            assert state["removed"]
        return SimpleNamespace(returncode=1 if command[1] == "inspect" else 0)

    def validate(root, saved, **kwargs):
        assert root == output and kwargs["sequence"] == 1
        assert (root / saved["archive"]).read_bytes() == b"original verified snapshot"
        if state["modified"]:
            raise WorkspaceError("outcome unknown")

    monkeypatch.setattr(faults, "DockerToolBackend", Backend)
    monkeypatch.setattr(faults.subprocess, "check_output", lambda *a, **k: "commit\n")
    monkeypatch.setattr(faults.subprocess, "run", run)
    monkeypatch.setattr(faults, "validate_batch", validate)
    monkeypatch.setattr(faults.docker_backend, "_run", lambda *a, **k: None)
    if violation:
        with pytest.raises(AssertionError):
            faults.run_snapshot_faults(output, tmp_path, "pinned")
        assert not (output / "faults.json").exists()
    else:
        summary = faults.run_snapshot_faults(output, tmp_path, "pinned")
        assert summary["passed"] and summary["provider_calls"] == 0
        assert summary["container_removed"] and len(summary["faults"]) == 6
        assert {row["fault"] for row in summary["faults"]} >= {
            "previous-batch", "unknown-modification", "cleanup-failure",
        }
