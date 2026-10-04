"""Verifier contract checks supplement, and never replace, real Docker replay evidence."""

import json
from types import SimpleNamespace

import pytest

from tracefix import docker_recovery_faults
from tracefix import docker_recovery_replay as replay


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
