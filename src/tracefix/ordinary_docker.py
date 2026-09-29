"""Build a deliberately prepared, immutable ordinary Python task image."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import uuid4

from tracefix.docker_backend import DockerToolBackend
from tracefix.runtime import RunConfig, TraceFixRunner


def prepare_image(requirements: Path | None, output: Path) -> dict[str, Any]:
    """Build once with dependencies, then hand `run` a verifiable image ID."""
    if shutil.which("docker") is None:
        raise ValueError("Docker CLI is required for ordinary image preparation")
    destination = output.expanduser().resolve()
    if destination.exists():
        raise ValueError("Docker build manifest already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    project_lock = b"" if requirements is None else requirements.expanduser().resolve().read_bytes()
    resource_root = files("tracefix").joinpath("docker")
    dockerfile = resource_root.joinpath("ordinary.Dockerfile").read_bytes()
    base_lock = resource_root.joinpath("ordinary-base.lock").read_bytes()
    identity = hashlib.sha256(dockerfile + b"\0" + base_lock + b"\0" + project_lock).hexdigest()
    tag = f"tracefix-ordinary:{identity[:16]}"
    build_log = destination.with_suffix(".build.log")
    with tempfile.TemporaryDirectory(prefix="tracefix-ordinary-image-") as temporary:
        context = Path(temporary)
        (context / "Dockerfile").write_bytes(dockerfile)
        (context / "ordinary-base.lock").write_bytes(base_lock)
        (context / "project-requirements.lock").write_bytes(project_lock)
        try:
            build = subprocess.run(
                ["docker", "build", "--pull=false", "-t", tag, str(context)],
                capture_output=True, text=True, timeout=900, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            build_log.write_text(str(exc) + "\n", encoding="utf-8")
            raise ValueError(f"ordinary Docker build could not complete: {exc}") from exc
        build_log.write_text(build.stdout + build.stderr, encoding="utf-8")
        if build.returncode:
            raise ValueError(f"ordinary Docker build failed; see {build_log}")
    inspected = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
        capture_output=True, text=True, timeout=20, check=False,
    )
    image_id = inspected.stdout.strip()
    if inspected.returncode or not image_id.startswith("sha256:") or len(image_id) != 71:
        raise ValueError("ordinary Docker image inspection failed")
    probe = subprocess.run(
        [
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "--user", "10001:10001", image_id, "python", "-c",
            "import platform,pytest; print(platform.python_version()); print(pytest.__version__)",
        ],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if probe.returncode or len(probe.stdout.splitlines()) != 2:
        raise ValueError(f"ordinary Docker image preflight failed: {probe.stderr[-1000:]}")
    report = {
        "schema_version": 1,
        "image_id": image_id,
        "tag": tag,
        "dockerfile_sha256": hashlib.sha256(dockerfile).hexdigest(),
        "base_lock_sha256": hashlib.sha256(base_lock).hexdigest(),
        "project_lock_sha256": hashlib.sha256(project_lock).hexdigest(),
        "python_version": probe.stdout.splitlines()[0],
        "pytest_version": probe.stdout.splitlines()[1],
        "build_log": str(build_log),
        "build_log_sha256": hashlib.sha256(build_log.read_bytes()).hexdigest(),
    }
    temporary_manifest = destination.with_suffix(".tmp")
    temporary_manifest.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary_manifest, destination)
    return report


def verify_docker_patch(run_dir: Path, config: RunConfig,
                        manifest: dict[str, Any], result: dict[str, Any],
                        patch: bytes) -> dict[str, Any]:
    """Reapply the recorded patch in a new container using the same image ID."""
    if config.docker_profile != "ordinary" or not config.docker_image_id:
        raise ValueError("Docker independent verification requires the ordinary profile")
    if not config.test_target or not config.source_import:
        raise ValueError("Docker verification requires a test target and import probe")
    image_id = config.docker_image_id
    prep = result.get("workspace_preparation", {})
    if prep.get("profile") != "ordinary" or prep.get("image_id") != image_id:
        raise ValueError("Docker image identity does not match the run")
    if prep.get("test_target") != config.test_target:
        raise ValueError("recorded test target changed")
    source, commit = TraceFixRunner._validate_source_repository(config.repo)
    if commit != result.get("source_commit") or commit != manifest["identity"]["source_commit"]:
        raise ValueError("source commit does not match the run")
    patch_hash = hashlib.sha256(patch).hexdigest()
    if not patch or patch_hash != result.get("patch_sha256"):
        raise ValueError("patch is empty or its identity changed")
    validation_path = run_dir / "independent-validation.json"
    if validation_path.exists():
        raise ValueError("independent validation already exists")

    with tempfile.TemporaryDirectory(prefix="tracefix-docker-verify-") as temporary:
        verification_root = Path(temporary)
        backend = DockerToolBackend(
            task_id="tracefix-ordinary", input_root=verification_root,
            run_dir=verification_root, run_id=f"verify-{uuid4().hex}",
            timeout_seconds=min(120, int(config.agent_config.wall_time_seconds)),
            profile="ordinary", image_id=image_id,
        )
        try:
            registry = backend.prepare(
                commit, source, Path(__file__).resolve().parents[2],
                source_import_probe=config.source_import,
            )
            from tracefix.messages import ToolCall

            apply_result = registry.get("apply_patch").execute(ToolCall(
                id="independent-patch", name="apply_patch",
                arguments={"patch": patch.decode("utf-8")},
            ))
            if not apply_result.success:
                raise ValueError(f"patch could not be applied: {apply_result.error}")
            test_result = registry.get("run_tests").execute(ToolCall(
                id="independent-test", name="run_tests",
                arguments={"command": f"pytest -q {config.test_target}"},
            ))
            backend.export_evidence()
            record = {
                "schema_version": 1,
                "backend": "docker", "source_commit": commit,
                "patch_sha256": patch_hash, "test_target": config.test_target,
                "image_id": image_id, "preparation": backend.workspace_preparation,
                "test": test_result.model_dump(mode="json"),
                "passed": test_result.success and test_result.output.get("test_status") == "passed",
            }
        finally:
            backend.close(remove=True)
        evidence = verification_root / "test-evidence"
        if evidence.exists():
            shutil.copytree(evidence, run_dir / "independent-validation-evidence")
    validation_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record
