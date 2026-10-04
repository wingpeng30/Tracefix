"""Offline ordinary Docker recovery acceptance across real host processes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from tracefix.checkpoint import CheckpointStore
from tracefix.dialogue_replay import prepare_dialogue
from tracefix.memory import atomic_json
from tracefix.memory_replay import RecordedMemoryClient
from tracefix.models import LiteLLMAdapter
from tracefix.onboarding import verify_patch
from tracefix.regression_replay import forbid_live_access
from tracefix.runtime import RunConfig, TraceFixRunner


def worker(output: Path, image_id: str, case: str, phase: str) -> None:
    directory = output / case
    directory.mkdir(exist_ok=True)
    client = RecordedMemoryClient(recall=False)
    client.sequence = [
        (entry[0], {"command": "pytest -q tests/test_widget.py::test_positive"})
        if entry is not None and entry[0] == "run_tests" else entry for entry in client.sequence
    ]
    runner = TraceFixRunner(lambda config: LiteLLMAdapter(config, client=client))
    interrupt = {"read": 3, "patch": 4, "test": 5, "twice": 3}[case] if phase == "run" \
        else 6 if case == "twice" and phase == "resume-one" else None
    original = CheckpointStore.save

    def save(store, payload, **kwargs):
        saved = original(store, payload, **kwargs)
        if kwargs["sequence"] == interrupt:
            raise KeyboardInterrupt()
        return saved

    with forbid_live_access(), patch.object(CheckpointStore, "save", save):
        if phase == "run":
            result = runner.run(RunConfig(
                repo=output / "source", task="Fix pagination", output_dir=directory / "runs",
                model_name="offline/docker-recovery", env_file=None, execution_backend="docker",
                docker_profile="ordinary", docker_image_id=image_id, docker_recovery_enabled=True,
                source_import="widget", test_target="tests/test_widget.py::test_positive",
            ))
            atomic_json(directory / "session-location.json", {
                "run": str(Path(result.result_path).parent),
            })
        else:
            root = Path(json.loads((directory / "session-location.json").read_text(
                encoding="utf-8"))["run"])
            inspection = runner.inspect(root)
            if not inspection["resumable"]:
                raise AssertionError(inspection)
            client.calls = inspection["step_count"]
            result = runner.resume(root)
    expected = "interrupted" if interrupt is not None else "completed"
    if result.status != expected:
        raise AssertionError(result.model_dump(mode="json"))
    atomic_json(directory / f"{phase}.json", {
        "pid": os.getpid(), "provider_calls": 0, "status": result.status,
        "step_count": result.step_count,
        "container_id": result.workspace_preparation["container_id"],
        "result_path": result.result_path,
    })


def run_recovery_replay(output: Path, image_id: str) -> dict:
    output = output.expanduser().resolve()
    prepare_dialogue(output)
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                   "PYTHONIOENCODING": "utf-8"}
    cases = []
    for case in ("read", "patch", "test", "twice"):
        phases = ("run", "resume-one", "resume-two") if case == "twice" else ("run", "resume")
        receipts = []
        for phase in phases:
            command = [sys.executable, "-m", "tracefix.docker_recovery_replay", "--output",
                       str(output), "--image-id", image_id, "--case", case, "--phase", phase]
            process = subprocess.run(command, cwd=output, env=environment, capture_output=True,
                                     timeout=300, check=False)
            (output / f"{case}-{phase}.stdout").write_bytes(process.stdout)
            (output / f"{case}-{phase}.stderr").write_bytes(process.stderr)
            if process.returncode:
                raise AssertionError(f"{case}/{phase} failed with exit {process.returncode}")
            receipt = json.loads((output / case / f"{phase}.json").read_text(encoding="utf-8"))
            removed = subprocess.run(["docker", "inspect", receipt["container_id"]],
                                     capture_output=True, timeout=30, check=False)
            if removed.returncode == 0:
                raise AssertionError("owned task container survived cleanup")
            receipts.append(receipt)
        if len({row["pid"] for row in receipts}) != len(receipts):
            raise AssertionError("recovery did not use independent host processes")
        if len({row["container_id"] for row in receipts}) != len(receipts):
            raise AssertionError("recovery did not recreate the container")
        root = Path(receipts[-1]["result_path"]).parent
        independent = verify_patch(root)
        atomic_json(output / case / "independent.json", independent)
        if not independent["passed"]:
            raise AssertionError(independent)
        cases.append({"case": case, "processes": receipts, "independent_passed": True})
    evidence = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in output.rglob("*.json") if ".git" not in path.parts}
    summary = {"accepted": True, "provider_calls": 0, "image_id": image_id, "cases": cases,
               "implementation_sha256": TraceFixRunner._implementation_sha256(),
               "evidence_sha256": evidence, "simulated_model_usage": True}
    atomic_json(output / "recovery-summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--case", choices=("read", "patch", "test", "twice"))
    parser.add_argument("--phase", choices=("run", "resume", "resume-one", "resume-two"))
    args = parser.parse_args()
    if args.case:
        if not args.phase:
            parser.error("a worker case requires --phase")
        worker(args.output, args.image_id, args.case, args.phase)
    else:
        print(json.dumps(run_recovery_replay(args.output, args.image_id), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
