"""Audit isolation and cleanup evidence from the public Docker smoke run."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any


def audit(
    root: Path,
    expected_image_id: str,
    *,
    inspect: Callable[[str], subprocess.CompletedProcess[bytes]] | None = None,
) -> dict[str, Any]:
    """Check both smoke arms against recorded container identity and live cleanup."""
    if inspect is None:
        def inspect(container_id: str) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                ["docker", "inspect", container_id], capture_output=True, check=False
            )
    arms: dict[str, Any] = {}
    source_commits: set[str] = set()
    package_hashes: set[str] = set()
    for name, skills_enabled in (("baseline", False), ("skills", True)):
        report_path = root / name / "reproduction.json"
        if not report_path.is_file():
            raise ValueError(f"missing Docker smoke report: {report_path}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        prep = report.get("workspace_preparation") or {}
        container_id = prep.get("container_id")
        source_commit = prep.get("source_commit")
        if report.get("execution_backend") != "docker":
            raise ValueError(f"{name} arm did not use Docker")
        if (
            report.get("docker_image_id") != expected_image_id
            or prep.get("image_id") != expected_image_id
        ):
            raise ValueError(f"{name} arm used an unexpected Docker image")
        if not isinstance(container_id, str) or not container_id:
            raise ValueError(f"{name} arm has no container identity")
        if prep.get("container_network_mode") != "none" or prep.get("container_mounts") != []:
            raise ValueError(f"{name} arm isolation inspection did not pass")
        if not isinstance(source_commit, str) or not source_commit:
            raise ValueError(f"{name} arm has no source commit identity")
        if report.get("skills_enabled") is not skills_enabled:
            raise ValueError(f"{name} arm Skills setting is inconsistent")
        if report.get("status") != "completed":
            raise ValueError(f"{name} arm did not complete")
        if (
            report.get("provider_client_constructions") != 0
            or report.get("provider_request_attempts") != 0
        ):
            raise ValueError(f"{name} arm attempted a provider call")
        if report.get("network_connect_attempts") != 0:
            raise ValueError(f"{name} arm attempted a network connection")
        trajectory = report.get("trajectory_validation") or {}
        expected_tool_results = 6 if skills_enabled else 5
        if (
            trajectory.get("model_requests", 0) <= 0
            or trajectory.get("tool_results") != expected_tool_results
            or trajectory.get("pytest_returncode") != 0
            or not trajectory.get("diff_sha256")
        ):
            raise ValueError(f"{name} arm trajectory or test evidence is incomplete")
        package_hash = report.get("tracefix_source_tree_sha256")
        if not isinstance(package_hash, str) or len(package_hash) != 64:
            raise ValueError(f"{name} arm has no TraceFix package tree hash")
        package_hashes.add(package_hash)
        activations = report.get("skill_activations") or []
        if skills_enabled:
            if not activations or any(
                not isinstance(item.get("content_sha256"), str)
                or len(item["content_sha256"]) != 64
                for item in activations
            ):
                raise ValueError("Skills arm has no valid activated skill identity")
        elif activations:
            raise ValueError("baseline arm unexpectedly activated a skill")
        live = inspect(container_id)
        if live.returncode == 0:
            raise ValueError(f"{name} container still exists after the smoke run")
        detail = (live.stdout + live.stderr).decode("utf-8", "replace").casefold()
        if "no such object" not in detail:
            raise ValueError(f"could not verify removal of {name} container: {detail[-500:]}")
        source_commits.add(source_commit)
        arms[name] = {
            "container_id": container_id,
            "container_removed": True,
            "source_commit": source_commit,
            "skills_enabled": skills_enabled,
            "provider_calls": 0,
        }
    if len(source_commits) != 1:
        raise ValueError("Docker smoke arms used different source commits")
    if len(package_hashes) != 1:
        raise ValueError("Docker smoke arms used different TraceFix package trees")
    return {"accepted": True, "image_id": expected_image_id, "arms": arms}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.root, args.image_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
