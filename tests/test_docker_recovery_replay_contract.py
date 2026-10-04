"""Verifier contract checks supplement, and never replace, real Docker replay evidence."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix import docker_recovery_faults
from tracefix import docker_recovery_replay as replay
from tracefix.checkpoint import CheckpointStore


@pytest.mark.parametrize("case,phase,violation", [
    ("read", "run", None), ("patch", "run", None), ("test", "run", None),
    ("twice", "resume-one", None), ("read", "resume", None),
    ("read", "resume", "unsafe"), ("read", "resume", "terminal"),
])
def test_worker_requires_safe_resume_and_expected_durable_termination(
    tmp_path, monkeypatch, case, phase, violation,
):
    directory = tmp_path / case
    directory.mkdir()
    root = directory / "run"
    root.mkdir()
    (directory / "session-location.json").write_text(
        json.dumps({"run": str(root)}), encoding="utf-8")
    resumed = []

    class Result(SimpleNamespace):
        def model_dump(self, **kwargs):
            return vars(self)

    class Runner:
        def __init__(self, factory):
            assert callable(factory)

        def inspect(self, requested):
            assert requested == root
            return {"resumable": violation != "unsafe", "step_count": 2}

        def finish(self, sequence):
            store = CheckpointStore(root, {"worker": case})
            status = "completed"
            try:
                store.save({"safe_batch": True}, sequence=sequence)
            except KeyboardInterrupt:
                status = "interrupted"
            if violation == "terminal":
                status = "failed"
            return Result(status=status, step_count=sequence - 1,
                          result_path=str(root / "result.json"),
                          workspace_preparation={"container_id": "new-owned-container"})

        def run(self, config):
            assert config.docker_recovery_enabled and config.docker_profile == "ordinary"
            return self.finish({"read": 3, "patch": 4, "test": 5}[case])

        def resume(self, requested):
            resumed.append(requested)
            return self.finish(6)

    monkeypatch.setattr(replay, "TraceFixRunner", Runner)
    if violation:
        with pytest.raises(AssertionError):
            replay.worker(tmp_path, "sha256:" + "a" * 64, case, phase)
        assert not (directory / f"{phase}.json").exists()
        if violation == "unsafe":
            assert not resumed
    else:
        replay.worker(tmp_path, "sha256:" + "a" * 64, case, phase)
        receipt = json.loads((directory / f"{phase}.json").read_text(encoding="utf-8"))
        assert receipt["status"] == (
            "interrupted" if phase in {"run", "resume-one"} else "completed")
        assert receipt["provider_calls"] == 0
        saved = CheckpointStore(root, {"worker": case}).load()
        assert saved.payload == {"safe_batch": True}
        assert saved.sequence == receipt["step_count"] + 1
        assert Path(receipt["result_path"]).parent == root


@pytest.mark.parametrize("fault", [
    None, "worker", "container", "pid", "identity", "patch", "status", "provider", "faults",
])
def test_replay_rejects_incomplete_or_reused_acceptance_evidence(tmp_path, monkeypatch, fault):
    output = tmp_path / "acceptance"
    original_run = replay.subprocess.run
    workers = []

    def run(command, **kwargs):
        if command[0] == "git":
            return original_run(command, **kwargs)
        if command[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0 if fault == "container" else 1)
        case = command[command.index("--case") + 1]
        phase = command[command.index("--phase") + 1]
        workers.append((case, phase))
        directory = output / case
        directory.mkdir(exist_ok=True)
        root = directory / "run"
        root.mkdir(exist_ok=True)
        receipt = {
            "pid": 1 if fault == "pid" else len(workers),
            "provider_calls": 1 if fault == "provider" else 0,
            "container_id": "same" if fault == "identity" else f"container-{len(workers)}",
            "result_path": str(root / "result.json"),
            "status": "failed" if fault == "status" else
            "interrupted" if phase in {"run", "resume-one"} else "completed",
        }
        (directory / f"{phase}.json").write_text(json.dumps(receipt), encoding="utf-8")
        return SimpleNamespace(returncode=1 if fault == "worker" else 0,
                               stdout=b"worker evidence", stderr=b"worker diagnostic")

    verified = []

    def verify(root):
        verified.append(root)
        return {"passed": fault != "patch"}

    monkeypatch.setattr(replay.subprocess, "run", run)
    monkeypatch.setattr(replay, "verify_patch", verify)
    monkeypatch.setattr(docker_recovery_faults, "run_snapshot_faults", lambda *args: {
        "passed": fault != "faults", "provider_calls": 0,
    })
    if fault:
        with pytest.raises(AssertionError):
            replay.run_recovery_replay(output, "pinned-image")
        assert not (output / "recovery-summary.json").exists()
    else:
        summary = replay.run_recovery_replay(output, "pinned-image")
        assert summary["accepted"] and summary["provider_calls"] == 0
        assert len(workers) == 9 and len(verified) == 4
        assert [row["case"] for row in summary["cases"]] == ["read", "patch", "test", "twice"]
        assert all(row["independent_passed"] for row in summary["cases"])
        assert summary["evidence_sha256"]
        assert len(summary["implementation_sha256"]) == 64
