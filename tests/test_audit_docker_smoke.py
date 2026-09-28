from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.audit_docker_smoke import audit


def _write_arm(root: Path, arm: str, *, skills: bool, commit: str = "abc123") -> None:
    target = root / arm
    target.mkdir(parents=True)
    report = {
        "execution_backend": "docker",
        "docker_image_id": "sha256:" + "a" * 64,
        "tracefix_source_tree_sha256": "b" * 64,
        "skills_enabled": skills,
        "status": "completed",
        "provider_client_constructions": 0,
        "provider_request_attempts": 0,
        "network_connect_attempts": 0,
        "skill_activations": ([{"content_sha256": "c" * 64}] if skills else []),
        "trajectory_validation": {
            "model_requests": 6 if skills else 5,
            "tool_results": 6 if skills else 5,
            "pytest_returncode": 0,
            "diff_sha256": "d" * 64,
        },
        "workspace_preparation": {
            "container_id": f"container-{arm}",
            "image_id": "sha256:" + "a" * 64,
            "source_commit": commit,
            "container_network_mode": "none",
            "container_mounts": [],
        },
    }
    (target / "reproduction.json").write_text(json.dumps(report), encoding="utf-8")


def test_audit_requires_isolation_provider_zero_and_container_cleanup(tmp_path):
    image = "sha256:" + "a" * 64
    _write_arm(tmp_path, "baseline", skills=False)
    _write_arm(tmp_path, "skills", skills=True)
    inspected: list[str] = []

    def inspect(container_id: str) -> subprocess.CompletedProcess[bytes]:
        inspected.append(container_id)
        return subprocess.CompletedProcess(
            ["docker", "inspect", container_id],
            1,
            b"",
            b"Error: No such object: " + container_id.encode(),
        )

    result = audit(tmp_path, image, inspect=inspect)
    assert result["accepted"] is True
    assert inspected == ["container-baseline", "container-skills"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("container_network_mode", "bridge", "isolation"),
        ("container_mounts", [{"Type": "bind"}], "isolation"),
    ],
)
def test_audit_rejects_unisolated_container(tmp_path, field, value, message):
    image = "sha256:" + "a" * 64
    _write_arm(tmp_path, "baseline", skills=False)
    _write_arm(tmp_path, "skills", skills=True)
    path = tmp_path / "baseline" / "reproduction.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["workspace_preparation"][field] = value
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        audit(tmp_path, image, inspect=lambda container_id: subprocess.CompletedProcess([], 1))


def test_audit_rejects_container_that_survived_cleanup(tmp_path):
    image = "sha256:" + "a" * 64
    _write_arm(tmp_path, "baseline", skills=False)
    _write_arm(tmp_path, "skills", skills=True)
    with pytest.raises(ValueError, match="still exists"):
        audit(tmp_path, image, inspect=lambda container_id: subprocess.CompletedProcess([], 0))


def test_audit_rejects_docker_daemon_failure_as_cleanup_evidence(tmp_path):
    image = "sha256:" + "a" * 64
    _write_arm(tmp_path, "baseline", skills=False)
    _write_arm(tmp_path, "skills", skills=True)
    with pytest.raises(ValueError, match="could not verify removal"):
        audit(
            tmp_path,
            image,
            inspect=lambda container_id: subprocess.CompletedProcess(
                [], 1, b"", b"daemon unavailable"
            ),
        )
