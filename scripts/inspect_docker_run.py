"""Read-only identity and unresolved-call inspection for Docker-backed runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frozen_files_match(stage: Path, files: dict[str, str]) -> bool:
    """Verify every path and digest named by the frozen input manifest."""
    stage = stage.resolve()
    for relative, expected in files.items():
        try:
            path = (stage / relative).resolve(strict=True)
        except OSError:
            return False
        if stage not in path.parents or not path.is_file() or _sha256(path) != expected:
            return False
    return True


def persisted_tool_call_ids(events: list[dict[str, Any]]) -> set[str]:
    """Return call IDs whose result reached the durable Agent trajectory."""
    persisted: set[str] = set()
    for event in events:
        if event.get("event_type") != "tool_returned":
            continue
        result = event.get("payload", {}).get("result", {})
        call_id = result.get("call_id") if isinstance(result, dict) else None
        if isinstance(call_id, str) and call_id:
            persisted.add(call_id)
    return persisted


def unresolved_tool_calls(
    events: list[dict[str, Any]], persisted: set[str] | None = None
) -> dict[str, list[str]]:
    """Classify journal calls without replaying calls whose outcome is uncertain."""
    persisted = persisted or set()
    states: dict[str, str] = {}
    for event in events:
        call_id = event.get("call_id")
        kind = event.get("event")
        if not isinstance(call_id, str) or not call_id:
            continue
        if kind == "result_received":
            states[call_id] = "completed" if call_id in persisted else "not_persisted"
        elif kind in {"dispatch_started", "request_sent", "process_started", "outcome_unknown"}:
            states.setdefault(call_id, "unknown")
    return {
        "completed": sorted(call_id for call_id, state in states.items() if state == "completed"),
        "unknown": sorted(
            call_id for call_id, state in states.items() if state in {"unknown", "not_persisted"}
        ),
        "result_received_not_persisted": sorted(
            call_id for call_id, state in states.items() if state == "not_persisted"
        ),
    }


def inspect_run(run_dir: Path, docker: str = "docker") -> dict[str, Any]:
    state_path = run_dir / "docker-run-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("schema_version") != 1:
        raise ValueError("unsupported Docker run state schema")
    task_id = state.get("task_id")
    default_inputs = (
        Path(__file__).resolve().parents[1]
        / "runs"
        / "docker-foundation-20260926-v1"
        / "inputs-v2"
    )
    input_root = Path(state.get("input_root", default_inputs)).resolve(strict=True)
    stage = (input_root / task_id).resolve(strict=True)
    if input_root not in stage.parents:
        raise ValueError("frozen input path escaped its root")
    manifest_path = stage / "input-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    recipe_path = stage / "recipes" / f"{task_id}.json"
    result_path = run_dir / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else None
    identity_checks = {
        "input_manifest_hash": _sha256(manifest_path) == state.get("input_manifest_sha256"),
        "frozen_input_files": frozen_files_match(stage, manifest.get("files", {})),
        "source_commit": manifest.get("source_commit") == state.get("source_commit"),
        "recipe_fingerprint": _sha256(recipe_path) == state.get("recipe_fingerprint"),
        "result_identity": result is None
        or (
            result.get("run_id") == state.get("run_id")
            and result.get("source_commit") == state.get("source_commit")
        ),
    }
    container_present = False
    container_checks: dict[str, bool] = {}
    container_state = "unknown"
    recovery_classification = "identity_mismatch"
    container_id = state.get("container_id")
    if container_id:
        completed = subprocess.run(
            [docker, "inspect", container_id], capture_output=True, check=False
        )
        if completed.returncode == 0:
            container_present = True
            data = json.loads(completed.stdout.decode("utf-8"))[0]
            container_state = data.get("State", {}).get("Status", "unknown")
            labels = data.get("Config", {}).get("Labels") or {}
            container_checks = {
                "container_id": data.get("Id") == container_id,
                "image_id": data.get("Image") == state.get("image_id"),
                "run_label": labels.get("tracefix.run_id") == state.get("run_id"),
                "container_name": data.get("Name", "").lstrip("/")
                == state.get("container_name"),
                "no_host_mounts": not data.get("Mounts"),
            }
            recovery_classification = (
                "container_stopped"
                if container_state != "running"
                else "identity_mismatch"
                if not all(container_checks.values())
                else "container_running"
            )
        else:
            error_text = completed.stderr.decode("utf-8", "replace").casefold()
            missing = "no such object" in error_text or "no such container" in error_text
            unavailable = any(
                marker in error_text
                for marker in (
                    "error during connect",
                    "cannot connect",
                    "connection refused",
                    "is the docker daemon running",
                    "failed to connect",
                )
            )
            if unavailable:
                recovery_classification = "docker_unavailable"
                container_checks = {"docker_available": False}
            elif missing:
                if state.get("phase") == "completed":
                    recovery_classification = "completed_without_container"
                else:
                    recovery_classification = "container_missing"
                    container_checks = {"container_present": False}
            else:
                recovery_classification = "docker_inspect_error"
                container_checks = {"container_inspect_succeeded": False}
    elif state.get("phase") != "completed":
        container_checks = {"container_id_recorded": False}
    events_path = run_dir / "tool-events.jsonl"
    events = (
        [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
        if events_path.is_file()
        else []
    )
    trajectory_path = run_dir / "trajectory.jsonl"
    if not trajectory_path.is_file() and result is not None:
        trace_path = result.get("trace_path")
        if isinstance(trace_path, str):
            candidate = Path(trace_path)
            if candidate.is_file():
                trajectory_path = candidate
    trajectory = (
        [json.loads(line) for line in trajectory_path.read_text(encoding="utf-8").splitlines()]
        if trajectory_path.is_file()
        else []
    )
    calls = unresolved_tool_calls(events, persisted_tool_call_ids(trajectory))
    if calls["result_received_not_persisted"]:
        recovery_classification = "result_received_not_persisted"
    return {
        "run_id": state.get("run_id"),
        "task_id": task_id,
        "phase": state.get("phase"),
        "container_present": container_present,
        "container_state": container_state,
        "recovery_classification": recovery_classification,
        "identity_checks": identity_checks | container_checks,
        "identity_verified": all(identity_checks.values()) and all(container_checks.values()),
        "tool_calls": calls,
        "recovery_policy": {
            "completed_calls_reusable": True,
            "unknown_calls_replayed": False,
            "automatic_resume": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--docker", default="docker")
    args = parser.parse_args()
    report = inspect_run(args.run_dir.resolve(strict=True), args.docker)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    safe_terminal = report["recovery_classification"] in {
        "container_running",
        "completed_without_container",
    }
    return 0 if report["identity_verified"] and safe_terminal else 2


if __name__ == "__main__":
    raise SystemExit(main())
