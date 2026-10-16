"""Docker workspace and RPC-backed repository tools for fixed execution profiles.

Frozen tasks use audited recipes; ordinary Python tasks require a prepared immutable
image ID. Neither profile mounts the host repository or forwards its environment.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from tracefix.capabilities import (
    CallerOrigin,
    CapabilityCatalog,
    CapabilityError,
    CapabilityPolicy,
    ExecutionProfile,
    ToolDispatcher,
    executor_policy_document,
    host_control_policy,
)
from tracefix.docker_tls import write_test_tls_material
from tracefix.exceptions import RunConfigurationError, WorkspaceError
from tracefix.jobs.artifact_seed import JobArtifactSeed
from tracefix.jobs.protocol import JobWorkerSettings
from tracefix.messages import ToolCall
from tracefix.repository import RepoMap, RepoMapConfig
from tracefix.security import SECURITY_POLICY_VERSION, validate_project_environment
from tracefix.skill_snapshot import (
    EMPTY_SKILL_BUNDLE_SHA256,
    SkillBundleSnapshot,
    snapshot_custom_skills,
)
from tracefix.tools import BaseTool, SkillCatalogEntry, ToolRegistry, ToolResult, ToolSpec
from tracefix.tools.skills import SkillLimits

_IMAGES: dict[str, tuple[str, str]] = {
    "pytest-dev__pytest-10081": (
        "sha256:1e2488a5c0e112771dec47fb1d07bd405c8c68e6973d1e91a8d85cf87c384c64",
        "/opt/python310/bin/python3.10",
    ),
    "psf__requests-1766": (
        "sha256:c35e52584fb34cbb6eef9fd2ed073e166beec0467b37882938d6f8292516579e",
        "/opt/python39/bin/python3.9",
    ),
    "sphinx-doc__sphinx-10449": (
        "sha256:ff6e2792ee57fb20693939326b3d463915c72466a1f8a383689478935961acdc",
        "/opt/python310/bin/python3.10",
    ),
}
FROZEN_DOCKER_IMAGES = {name: item[0] for name, item in _IMAGES.items()}


def _make_trusted_executor_bundle(
    package_root: Path,
    tracefix_root: Path,
    environment: dict[str, str],
    *,
    include_tls_test_support: bool,
    capability_policy_document: dict[str, object],
    skills_root: Path | None = None,
    skills_snapshot: SkillBundleSnapshot | None = None,
) -> tuple[bytes, str, str | None]:
    """Package current harness code and fixed config for root-owned extraction."""
    archive_files: dict[str, bytes] = {}
    for source in sorted(package_root.rglob("*")):
        if source.is_symlink():
            raise WorkspaceError("trusted TraceFix package contains a symbolic link")
        if source.is_dir():
            continue
        relative = source.relative_to(package_root)
        if "__pycache__" in relative.parts or source.suffix == ".pyc":
            continue
        if not source.is_file():
            continue
        archive_files[f"src/tracefix/{relative.as_posix()}"] = source.read_bytes()
    archive_files["runtime/environment.json"] = json.dumps(
        validate_project_environment(environment), ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    archive_files["runtime/capability-policy.json"] = json.dumps(
        capability_policy_document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if skills_root is not None and skills_snapshot is not None:
        raise WorkspaceError("custom Skills root and frozen snapshot cannot both be provided")
    if skills_root is not None:
        skills_snapshot = snapshot_custom_skills(skills_root)
    if skills_snapshot is not None:
        for relative, data in sorted(skills_snapshot.files.items()):
            archive_files[f"runtime/skills/{relative}"] = data
    tls_certificate_sha256 = None
    if include_tls_test_support:
        support = tracefix_root / "docker" / "requests-test-tls"
        for name in ("service.py", "sitecustomize.py"):
            source = support / name
            if source.is_file():
                if source.is_symlink():
                    raise WorkspaceError("trusted test service contains a symbolic link")
                archive_files[f"request-test/{name}"] = source.read_bytes()
        with tempfile.TemporaryDirectory(prefix="tracefix-tls-") as temporary:
            certificate = Path(temporary) / "test-ca.pem"
            private_key = Path(temporary) / "test-key.pem"
            write_test_tls_material(certificate, private_key)
            certificate_bytes = certificate.read_bytes()
            tls_certificate_sha256 = hashlib.sha256(certificate_bytes).hexdigest()
            archive_files["request-test/test-ca.pem"] = certificate_bytes
            archive_files["request-test/test-key.pem"] = private_key.read_bytes()

    hashes = {
        name: hashlib.sha256(data).hexdigest() for name, data in sorted(archive_files.items())
    }
    canonical = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    bundle_sha256 = hashlib.sha256(canonical).hexdigest()
    manifest = json.dumps(
        {
            "security_policy_version": SECURITY_POLICY_VERSION,
            "bundle_sha256": bundle_sha256,
            "files": hashes,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    archive_files["runtime/executor-manifest.json"] = manifest
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, data in sorted(archive_files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o644
            member.mtime = 0
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue(), bundle_sha256, tls_certificate_sha256


_MAX_FRAME = 64 * 1024 * 1024


def _validate_synthetic_profile(
    task_id: str, recipe: dict[str, Any], manifest: dict[str, Any]
) -> None:
    allowed = {"task_id", "environment_variables"}
    if (
        task_id != "tracefix-synthetic"
        or set(recipe) - allowed
        or recipe.get("task_id") != task_id
        or recipe.get("environment_variables", {}) != {}
        or manifest.get("files", {}).keys() != {"source.bundle"}
    ):
        raise WorkspaceError("synthetic Docker recipe exceeds the fixed smoke profile")


def _run(
    command: list[str], *, input_data: bytes | str | None = None, timeout: float = 60
) -> subprocess.CompletedProcess[Any]:
    try:
        result = subprocess.run(
            command,
            input=input_data,
            capture_output=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceError(f"Docker backend command failed to start: {command[0]}") from exc
    if result.returncode:
        stderr = (
            result.stderr.decode("utf-8", "replace")
            if isinstance(result.stderr, bytes)
            else result.stderr
        )
        raise WorkspaceError(
            "Docker backend command failed",
            context={
                "command": command[:4],
                "returncode": result.returncode,
                "stderr": (stderr or "")[-4000:],
            },
        )
    return result


class _BridgeSession:
    """Bounded JSONL session; protocol events are separated from test output."""

    def __init__(
        self,
        command: list[str],
        timeout: float,
        journal_path: Path,
        *,
        skills_enabled: bool = False,
        artifacts_enabled: bool = False,
        execution_profile: ExecutionProfile = ExecutionProfile.WORKSPACE_WRITE,
        executor_policy_config_sha256: str | None = None,
        job_protocol: dict[str, Any] | None = None,
        repository_rules_enabled: bool = False,
    ) -> None:
        expected_jobs = None
        if job_protocol is not None:
            from tracefix.jobs.protocol import JobProtocolHello

            try:
                expected_jobs = JobProtocolHello.model_validate(job_protocol)
            except ValidationError as exc:
                raise WorkspaceError("invalid expected Docker job protocol") from exc
            if not artifacts_enabled:
                raise WorkspaceError("Docker jobs require bounded artifacts")
        self.timeout = timeout
        self.journal_path = journal_path
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.frames: queue.Queue[bytes | BaseException] = queue.Queue()
        self.stderr_tail = bytearray()
        self._lock = threading.Lock()
        self._closed = False
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            hello = self._next_frame(timeout)
        except BaseException:
            self.close(force=True)
            raise
        try:
            handshake = json.loads(hello)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.close(force=True)
            raise WorkspaceError("invalid Docker tool bridge handshake") from exc
        if (
            not isinstance(handshake, dict)
            or handshake.get("type") != "hello"
            or handshake.get("protocol") != 1
            or not isinstance(handshake.get("tools"), list)
        ):
            self.close(force=True)
            raise WorkspaceError("Docker tool bridge protocol identity mismatch")
        raw_catalog = handshake.get("skill_catalog", [])
        if not isinstance(raw_catalog, list):
            self.close(force=True)
            raise WorkspaceError("Docker tool bridge skill catalog is malformed")
        try:
            catalog = tuple(SkillCatalogEntry.model_validate(item) for item in raw_catalog)
        except ValidationError as exc:
            self.close(force=True)
            raise WorkspaceError("Docker tool bridge skill catalog identity is invalid") from exc
        if len({item.name for item in catalog}) != len(catalog):
            self.close(force=True)
            raise WorkspaceError("Docker tool bridge skill catalog contains duplicate names")
        try:
            specs = tuple(ToolSpec.model_validate(item) for item in handshake["tools"])
        except ValidationError as exc:
            self.close(force=True)
            raise WorkspaceError("Docker tool bridge tool schema is invalid") from exc
        if len({spec.name for spec in specs}) != len(specs):
            self.close(force=True)
            raise WorkspaceError("Docker tool bridge contains duplicate tool names")
        try:
            from tracefix.jobs.constants import JOB_CONTROL_CAPABILITIES

            raw_names = tuple(spec.name for spec in specs)
            if any(name in {*JOB_CONTROL_CAPABILITIES, "repository_rules_read"}
                   or name.startswith("__") for name in raw_names):
                raise CapabilityError("executor directory contains private controls")
            capability_catalog = CapabilityCatalog.for_tool_names((
                *raw_names, *(JOB_CONTROL_CAPABILITIES if expected_jobs is not None else ()),
                *(("repository_rules_read",) if repository_rules_enabled else ())))
            if any(capability_catalog.get(name).execution_location in {"host", "mcp"}
                   for name in raw_names):
                raise CapabilityError("executor directory contains host or MCP tools")
            capability_policy = CapabilityPolicy(capability_catalog, execution_profile)
        except (CapabilityError, ValueError) as exc:
            self.close(force=True)
            raise WorkspaceError("Docker tool bridge capability catalog is invalid") from exc
        if (
            handshake.get("execution_profile") != capability_policy.profile.value
            or handshake.get("capability_catalog_sha256") != capability_catalog.sha256
            or handshake.get("capability_policy_sha256") != capability_policy.sha256
            or handshake.get("executor_policy_config_sha256")
            != executor_policy_config_sha256
        ):
            self.close(force=True)
            raise WorkspaceError("Docker tool bridge capability policy negotiation failed")
        actual_jobs = handshake.get("job_protocol")
        if expected_jobs is None:
            if actual_jobs is not None:
                self.close(force=True)
                raise WorkspaceError("Docker bridge exposed jobs while disabled")
        else:
            from tracefix.jobs.protocol import JobProtocolHello

            try:
                actual_jobs = JobProtocolHello.model_validate(actual_jobs)
                if actual_jobs != expected_jobs:
                    raise ValueError("job worker identity drift")
            except (ValidationError, ValueError) as exc:
                self.close(force=True)
                raise WorkspaceError("Docker job protocol negotiation failed") from exc
        self.job_protocol = (expected_jobs.model_dump(mode="json")
                             if expected_jobs is not None else None)
        actual_rules = handshake.get("repository_rules_protocol_version")
        rules_match = (type(actual_rules) is int and actual_rules == 1
                       if repository_rules_enabled else actual_rules is None)
        if not rules_match:
            self.close(force=True)
            raise WorkspaceError("Docker repository rules protocol negotiation failed")
        self.repository_rules_enabled = repository_rules_enabled
        artifact_protocol = handshake.get("artifact_protocol")
        expected_artifact_protocol = {
            "version": 1,
            "max_artifact_bytes": 8 * 1024 * 1024,
            "max_run_bytes": 64 * 1024 * 1024,
            "max_run_artifacts": 128,
        }
        if artifacts_enabled and artifact_protocol != expected_artifact_protocol:
            self.close(force=True)
            raise WorkspaceError("Docker bridge artifact capability negotiation failed")
        if not artifacts_enabled and artifact_protocol is not None:
            self.close(force=True)
            raise WorkspaceError("Docker bridge exposed artifacts while disabled")
        load_specs = [spec for spec in specs if spec.name == "load_skill"]
        if skills_enabled:
            declared = (
                set(
                    load_specs[0].input_schema.get("properties", {}).get("name", {}).get("enum", [])
                )
                if len(load_specs) == 1
                else set()
            )
            if not catalog or declared != {item.name for item in catalog}:
                self.close(force=True)
                raise WorkspaceError("Docker skill catalog does not match the load_skill tool")
        elif catalog or load_specs:
            self.close(force=True)
            raise WorkspaceError("Docker bridge exposed skills while skills are disabled")
        self.run_id = handshake.get("run_id")
        self._record_event(
            {"event": "handshake", "protocol": handshake.get("protocol"), "run_id": self.run_id}
        )
        self.tools = specs
        self.execution_profile = capability_policy.profile
        self.capability_policy = capability_policy
        self.capability_catalog_sha256 = capability_catalog.sha256
        self.capability_policy_sha256 = capability_policy.sha256
        self.executor_policy_config_sha256 = executor_policy_config_sha256
        self.skill_catalog = catalog
        self.repo_map = (
            RepoMap.model_validate(handshake["repo_map"]) if handshake.get("repo_map") else None
        )

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            while True:
                line = self.process.stdout.readline(_MAX_FRAME + 1)
                if not line:
                    self.frames.put(EOFError("bridge stdout closed"))
                    return
                if len(line) > _MAX_FRAME or not line.endswith(b"\n"):
                    self.frames.put(ValueError("bridge response exceeded frame limit"))
                    return
                self.frames.put(line.rstrip(b"\r\n"))
        except BaseException as exc:
            self.frames.put(exc)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        while True:
            chunk = self.process.stderr.read(4096)
            if not chunk:
                return
            self.stderr_tail.extend(chunk)
            if len(self.stderr_tail) > 16_000:
                del self.stderr_tail[:-16_000]

    def _next_frame(self, timeout: float) -> bytes:
        try:
            frame = self.frames.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("Docker tool bridge response timed out") from exc
        if isinstance(frame, BaseException):
            detail = self.stderr_tail.decode("utf-8", "replace")[-2000:]
            raise WorkspaceError(f"Docker tool bridge stopped: {frame}; {detail}")
        return frame

    def call(
        self,
        name: str,
        arguments: dict[str, Any],
        on_process_started: Any = None,
        *,
        call_id: str | None = None,
    ) -> ToolResult:
        call_id = call_id or uuid4().hex
        with self._lock:
            if self._closed or self.process.poll() is not None:
                raise WorkspaceError("Docker tool bridge is not running")
            assert self.process.stdin is not None
            payload = (
                json.dumps(
                    {"id": call_id, "name": name, "arguments": arguments}, ensure_ascii=False
                ).encode("utf-8")
                + b"\n"
            )
            self._record_event(
                {
                    "call_id": call_id,
                    "tool": name,
                    "event": "dispatch_started",
                    "arguments_sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
            self.process.stdin.write(payload)
            self.process.stdin.flush()
            self._record_event({"call_id": call_id, "tool": name, "event": "request_sent"})
            call_timeout = self.timeout
            if name == "run_tests":
                requested_timeout = arguments.get("timeout_seconds")
                if isinstance(requested_timeout, (int, float)) and not isinstance(
                    requested_timeout, bool
                ):
                    # The RPC deadline must exceed pytest's own timeout so the
                    # remote tool can kill its process group and return evidence.
                    call_timeout = max(call_timeout, float(requested_timeout) + 30.0)
            deadline = time.monotonic() + call_timeout
            try:
                while True:
                    frame = self._next_frame(max(0.001, deadline - time.monotonic()))
                    try:
                        value = json.loads(frame)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise WorkspaceError("invalid Docker tool bridge response") from exc
                    if value.get("type") == "process_started":
                        if value.get("call_id") != call_id:
                            raise WorkspaceError("Docker tool bridge event call ID mismatch")
                        self._record_event(
                            {"call_id": call_id, "tool": name, "event": "process_started"}
                        )
                        if on_process_started is not None:
                            on_process_started()
                        continue
                    if value.get("type") != "result" or value.get("call_id") != call_id:
                        raise WorkspaceError("Docker tool bridge result identity mismatch")
                    try:
                        result = ToolResult.model_validate(value.get("result"))
                    except ValidationError as exc:
                        raise WorkspaceError("invalid tool result from Docker bridge") from exc
                    if result.call_id != call_id or result.tool_name != name:
                        raise WorkspaceError("Docker tool result does not match request")
                    self._record_event(
                        {
                            "call_id": call_id,
                            "tool": name,
                            "event": "result_received",
                            "success": result.success,
                        }
                    )
                    return result
            except BaseException as exc:
                self._record_event(
                    {
                        "call_id": call_id,
                        "tool": name,
                        "event": "outcome_unknown",
                        "error_type": type(exc).__name__,
                    }
                )
                raise

    def _record_event(self, event: dict[str, Any]) -> None:
        event.setdefault("run_id", getattr(self, "run_id", None))
        event["recorded_at"] = time.time()
        with self.journal_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()

    def close(self, force: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        if force and self.process.poll() is None:
            self.process.kill()
        elif self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


class _RemoteTool(BaseTool):
    def __init__(
        self,
        spec: ToolSpec,
        backend: DockerToolBackend,
        skill_catalog: tuple[SkillCatalogEntry, ...] = (),
    ) -> None:
        self._spec = spec
        self.backend = backend
        self._skill_catalog = skill_catalog if spec.name == "load_skill" else ()
        self.on_process_started = None

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def skill_catalog(self) -> tuple[SkillCatalogEntry, ...]:
        return self._skill_catalog

    def prepare(self, call: ToolCall) -> None:
        result = self.backend.session.call("__prepare__", call.model_dump(mode="json"))
        if not result.success:
            from tracefix.exceptions import ToolValidationError

            raise ToolValidationError(result.error or "test call rejected")

    def execute(self, call: ToolCall) -> ToolResult:
        return self.backend.session.call(
            call.name,
            call.arguments,
            self.on_process_started if call.name == "run_tests" else None,
            call_id=call.id,
        )


class DockerToolBackend:
    """Own one task container and expose repository tools over JSONL."""

    def __init__(
        self,
        *,
        task_id: str,
        input_root: Path,
        run_dir: Path,
        run_id: str,
        timeout_seconds: int = 120,
        profile: str = "frozen",
        image_id: str | None = None,
        execution_profile: ExecutionProfile = ExecutionProfile.WORKSPACE_WRITE,
    ) -> None:
        if profile == "frozen" and (task_id not in _IMAGES or image_id is not None):
            raise RunConfigurationError("Docker backend supports only frozen pilot task IDs")
        if profile == "synthetic" and (
            task_id != "tracefix-synthetic"
            or image_id is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
        ):
            raise RunConfigurationError(
                "synthetic Docker runs require their fixed task ID and image ID"
            )
        if profile == "ordinary" and (
            task_id != "tracefix-ordinary"
            or image_id is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
        ):
            raise RunConfigurationError("ordinary Docker requires its own fixed image ID")
        if profile not in {"frozen", "synthetic", "ordinary"}:
            raise RunConfigurationError("unknown Docker execution profile")
        self.profile = profile
        self.execution_profile = ExecutionProfile(execution_profile)
        self._host_control_policy = host_control_policy(self.execution_profile)
        self.requested_image_id = image_id
        self.task_id = task_id
        self.input_root = input_root.expanduser().resolve(strict=True)
        self.run_dir = run_dir
        self.run_id = run_id
        self.docker = os.environ.get("DOCKER", "docker")
        self.container_name = f"tracefix-{run_id.lower().replace('_', '-')[:40]}"
        self.container_id: str | None = None
        self._container_removed = False
        self.session: _BridgeSession | None = None
        self.timeout_seconds = timeout_seconds
        self.recipe: dict[str, Any] = {}
        self.workspace_preparation: dict[str, Any] = {}
        self.source_commit: str | None = None
        self.input_manifest_sha256: str | None = None
        self.executor_bundle_sha256: str | None = None
        self.executor_policy_config_sha256: str | None = None
        self.executor_write_probe = "not_run"
        self.artifacts_enabled = False
        self.artifact_environment_path: str | None = None
        self.skills_bundle_sha256 = EMPTY_SKILL_BUNDLE_SHA256
        self.background_jobs_enabled = False
        self.repository_rules_enabled = False
        self.job_settings: JobWorkerSettings | None = None
        self.job_context = None
        self.job_protocol: dict[str, Any] | None = None
        self._job_execution_guard = None

    def bind_job_guard(self, guard) -> None:
        """Host assembly may add a denying guard; policy grants never change."""
        if not self.background_jobs_enabled or self._job_execution_guard is not None:
            raise WorkspaceError("Docker job execution guard cannot be replaced")
        self._job_execution_guard = guard

    def _control_decision(self, name: str, origin: CallerOrigin):
        return ToolDispatcher(self._host_control_policy, execution_guard=getattr(
            self, "_job_execution_guard", None)).authorize(name, origin)

    def _authorize_source_export(self):
        decision = self._control_decision("source_snapshot_export", CallerOrigin.HOST_LIFECYCLE)
        if not decision.allowed:
            raise WorkspaceError("Docker source export denied",
                                 context={"reason_code": decision.reason_code})

    def repository_rule_ports(self):
        from tracefix.repository_rules.protocol import RemoteRulePorts

        if not self.repository_rules_enabled or self.session is None:
            raise WorkspaceError("repository rules were not negotiated for this executor")

        def authorize():
            dispatcher = ToolDispatcher(self.session.capability_policy,
                                        execution_guard=self._job_execution_guard)
            decision = dispatcher.authorize("repository_rules_read", CallerOrigin.HOST_LIFECYCLE)
            if not decision.allowed:
                raise WorkspaceError("Docker repository rule control denied",
                                     context={"reason_code": decision.reason_code})

        return RemoteRulePorts(self.session, authorize)

    def prepare(
        self,
        source_commit: str,
        source_repo: Path,
        tracefix_root: Path,
        *,
        repo_map_task: str | None = None,
        repo_map_config: RepoMapConfig | None = None,
        skills_enabled: bool = False,
        skill_limits: SkillLimits | None = None,
        source_import_probe: str | None = None,
        skills_root: Path | None = None,
        baseline_archive: Path | None = None,
        recovery_snapshot: dict[str, Any] | None = None,
        recovery_enabled: bool = False,
        artifacts_enabled: bool = False,
        environment_variables: dict[str, str] | None = None,
        execution_profile: ExecutionProfile | str | None = None,
        skills_snapshot: SkillBundleSnapshot | None = None,
        job_settings: JobWorkerSettings | None = None,
        artifact_seed: JobArtifactSeed | None = None,
        repository_rules_enabled: bool = False,
    ) -> ToolRegistry:
        self.artifacts_enabled = artifacts_enabled
        self.repository_rules_enabled = repository_rules_enabled
        if repository_rules_enabled and (self.profile != "ordinary" or not recovery_enabled):
            raise WorkspaceError("Docker repository rules require ordinary recovery")
        self.background_jobs_enabled = job_settings is not None
        if artifact_seed is not None and (not self.background_jobs_enabled
                                          or recovery_snapshot is not None):
            raise WorkspaceError("artifact seeding requires a fresh background job executor")
        if self.background_jobs_enabled:
            if self.profile != "ordinary" or not recovery_enabled or not artifacts_enabled:
                raise WorkspaceError("Docker jobs require ordinary recovery and artifacts")
            self.job_settings = JobWorkerSettings.model_validate_json(
                job_settings.model_dump_json())
        if skills_root is not None and skills_snapshot is not None:
            raise WorkspaceError("custom Skills root and frozen snapshot cannot both be provided")
        if skills_root is not None:
            if not skills_enabled:
                raise WorkspaceError("custom Skills require Skills to be enabled")
            skills_snapshot = snapshot_custom_skills(skills_root, limits=skill_limits)
        if skills_snapshot is not None and not skills_enabled:
            raise WorkspaceError("custom Skills snapshot requires Skills to be enabled")
        self.skills_bundle_sha256 = (
            skills_snapshot.sha256 if skills_snapshot is not None else EMPTY_SKILL_BUNDLE_SHA256
        )
        if execution_profile is not None:
            self.execution_profile = ExecutionProfile(execution_profile)
            self._host_control_policy = host_control_policy(self.execution_profile)
        self.source_commit = source_commit
        if self.profile == "ordinary":
            if not source_import_probe:
                raise WorkspaceError("ordinary Docker requires a source import probe")
            self.recipe = {
                "source_import_probe": source_import_probe,
                "environment_variables": validate_project_environment(environment_variables or {}),
            }
            manifest = {}
        else:
            stage = self.input_root / self.task_id
            bundle = stage / "source.bundle"
            recipe_path = stage / "recipes" / f"{self.task_id}.json"
            manifest_path = stage / "input-manifest.json"
            if not bundle.is_file() or not recipe_path.is_file() or not manifest_path.is_file():
                raise WorkspaceError("frozen Docker task input is incomplete")
            self.recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
            self.recipe["environment_variables"] = validate_project_environment(
                self.recipe.get("environment_variables", {})
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.input_manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            if (
                manifest.get("task_id") != self.task_id
                or manifest.get("source_commit") != source_commit
            ):
                raise WorkspaceError("frozen Docker input identity mismatch")
            if self.profile == "synthetic":
                _validate_synthetic_profile(self.task_id, self.recipe, manifest)
            for relative, expected in manifest.get("files", {}).items():
                path = (stage / relative).resolve(strict=True)
                if stage.resolve() not in path.parents or not path.is_file():
                    raise WorkspaceError("frozen Docker input path escaped its root")
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise WorkspaceError(
                        "frozen Docker input hash mismatch", context={"path": relative}
                    )
        if baseline_archive is not None:
            if (
                self.profile != "ordinary"
                or baseline_archive.is_symlink()
                or not baseline_archive.resolve().is_relative_to(self.run_dir.resolve())
                or not baseline_archive.is_file()
            ):
                raise WorkspaceError("invalid saved Docker source baseline")
            source = baseline_archive
        else:
            source = self._source_archive(source_repo, source_commit)
        if recovery_snapshot is not None:
            self.container_name += "-recovery-" + uuid4().hex[:8]
        if self.profile in {"synthetic", "ordinary"}:
            assert self.requested_image_id is not None
            expected_image, self.python = self.requested_image_id, "/usr/local/bin/python"
        else:
            expected_image, self.python = _IMAGES[self.task_id]
        actual_image = _run(
            [self.docker, "image", "inspect", "--format", "{{.Id}}", expected_image]
        )
        actual_id = actual_image.stdout.decode().strip()
        if actual_id != expected_image:
            raise WorkspaceError(
                "Docker image identity mismatch",
                context={"expected": expected_image, "actual": actual_id},
            )
        self.image_id = actual_id
        if self.profile == "ordinary":
            self.source_tag = f"tracefix-source-{source_commit[:12]}"
        else:
            tag_result = _run(
                ["git", "-C", str(source_repo), "describe", "--tags", "--abbrev=0", source_commit]
            )
            self.source_tag = tag_result.stdout.decode("utf-8", "replace").strip()
            if not self.source_tag:
                raise WorkspaceError("cannot determine version tag for frozen source commit")
            self._check_storage()
        _run(
            [
                self.docker,
                "create",
                "--name",
                self.container_name,
                *(["--init"] if self.background_jobs_enabled else []),
                "--label",
                f"tracefix.run_id={self.run_id}",
                "--network",
                "none",
                "--security-opt",
                "no-new-privileges",
                "--cap-drop",
                "ALL",
                "--pids-limit",
                "256" if self.profile == "ordinary" else "512",
                "--memory",
                "2g" if self.profile == "ordinary" else "6g",
                *(
                    [
                        "--cpus",
                        "1",
                        "--read-only",
                        "--user",
                        "10001:10001",
                        "--tmpfs",
                        "/tmp:rw,nosuid,size=512m,uid=10001,gid=10001",
                        "--tmpfs",
                        "/input:rw,nosuid,size=256m,uid=10001,gid=10001",
                        "--tmpfs",
                        "/work:rw,nosuid,size=1g,uid=10001,gid=10001",
                        "--tmpfs",
                        "/opt/tracefix:rw,nosuid,size=128m,uid=0,gid=0,mode=0755",
                    ]
                    if self.profile == "ordinary"
                    else []
                ),
                "--env",
                "HTTP_PROXY=",
                "--env",
                "HTTPS_PROXY=",
                "--env",
                "ALL_PROXY=",
                "--env",
                "http_proxy=",
                "--env",
                "https_proxy=",
                "--env",
                "all_proxy=",
                *(
                    ["--sysctl", "net.ipv4.ip_unprivileged_port_start=0"]
                    if self.recipe.get("test_pythonpath_entries")
                    else []
                ),
                *(["--entrypoint", "sleep"] if self.background_jobs_enabled else []),
                self.image_id,
                *(["infinity"] if self.background_jobs_enabled else ["sleep", "infinity"]),
            ]
        )
        inspect = _run([self.docker, "inspect", "--format", "{{.Id}}", self.container_name])
        self.container_id = inspect.stdout.decode().strip()
        try:
            network_mode = json.loads(
                _run(
                    [
                        self.docker,
                        "inspect",
                        "--format",
                        "{{json .HostConfig.NetworkMode}}",
                        self.container_id,
                    ]
                ).stdout.decode("utf-8", "strict")
            )
            mounts = json.loads(
                _run(
                    [self.docker, "inspect", "--format", "{{json .Mounts}}", self.container_id]
                ).stdout.decode("utf-8", "strict")
            )
            host_config = (
                json.loads(
                    _run(
                        [
                            self.docker,
                            "inspect",
                            "--format",
                            "{{json .HostConfig}}",
                            self.container_id,
                        ]
                    ).stdout.decode("utf-8", "strict")
                )
                if self.profile == "ordinary"
                else None
            )
            container_user = (
                _run([self.docker, "inspect", "--format", "{{.Config.User}}", self.container_id])
                .stdout.decode("utf-8", "strict")
                .strip()
                if self.profile == "ordinary"
                else None
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceError(
                "could not verify Docker container network and mounts",
                context={"detail": str(exc)},
            ) from exc
        if network_mode != "none":
            raise WorkspaceError(
                "synthetic Docker container must have networking disabled",
                context={"network_mode": network_mode},
            )
        if any(item.get("Type") != "tmpfs" for item in mounts):
            raise WorkspaceError(
                "Docker task container must not have host mounts",
                context={"mounts": mounts},
            )
        if self.profile == "ordinary" and (
            container_user != "10001:10001"
            or host_config.get("ReadonlyRootfs") is not True
            or host_config.get("PidsLimit") != 256
            or host_config.get("Memory") != 2 * 1024**3
            or host_config.get("NanoCpus") != 1_000_000_000
        ):
            raise WorkspaceError("ordinary Docker resource or user contract changed")
        if self.background_jobs_enabled and host_config.get("Init") is not True:
            raise WorkspaceError("Docker background jobs require a verified init process")
        if self.profile == "ordinary":
            tmpfs = host_config.get("Tmpfs") if isinstance(host_config, dict) else None
            opt_mount = tmpfs.get("/opt/tracefix", "") if isinstance(tmpfs, dict) else ""
            normalized_opt_mount = opt_mount.replace("mode=0755", "mode=755")
            if not all(token in normalized_opt_mount for token in ("uid=0", "gid=0", "mode=755")):
                raise WorkspaceError("ordinary Docker executor directory is not root-owned")
        self._write_run_state("container_created")
        _run([self.docker, "start", self.container_name])
        if self.background_jobs_enabled:
            self._capture_job_context()
        container_interpreter: dict[str, str] | None = None
        if self.profile == "ordinary":
            probe_result = _run(
                [
                    self.docker,
                    "exec",
                    self.container_name,
                    self.python,
                    "-c",
                    "import json,platform,pytest; print(json.dumps({"
                    "'python':platform.python_version(),'pytest':pytest.__version__}))",
                ]
            )
            container_interpreter = json.loads(probe_result.stdout.decode("utf-8"))
        _run(
            [
                self.docker,
                "exec",
                self.container_name,
                "mkdir",
                "-p",
                "/input",
                "/work",
                "/work/agent",
                "/work/evidence",
                "/work/artifact-evidence",
            ]
        )
        self._copy_file_into(source, "/input/source.tar")
        package_root = Path(__file__).resolve().parent
        if self.profile == "ordinary":
            environment = validate_project_environment(self.recipe.get("environment_variables", {}))
            policy_document = executor_policy_document(
                self.execution_profile,
                artifacts_enabled=artifacts_enabled,
                skills_enabled=skills_enabled,
                **({"jobs_enabled": True} if self.background_jobs_enabled else {}),
                repository_rules_enabled=self.repository_rules_enabled,
            )
            self.executor_policy_config_sha256 = hashlib.sha256(
                json.dumps(
                    policy_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            bundle, self.executor_bundle_sha256, tls_certificate_sha256 = (
                _make_trusted_executor_bundle(
                    package_root,
                    tracefix_root,
                    environment,
                    include_tls_test_support=bool(self.recipe.get("test_pythonpath_entries")),
                    capability_policy_document=policy_document,
                    skills_snapshot=skills_snapshot,
                )
            )
            self._install_trusted_executor(bundle, self.executor_bundle_sha256)
            if self.background_jobs_enabled:
                self._install_job_context()
        else:
            _run([self.docker, "exec", self.container_name, "mkdir", "-p", "/opt/tracefix/src"])
            _run(
                [
                    self.docker,
                    "cp",
                    str(package_root),
                    f"{self.container_name}:/opt/tracefix/src/tracefix",
                ]
            )
            tls_certificate_sha256 = None
            if self.recipe.get("test_pythonpath_entries"):
                _run(
                    [
                        self.docker,
                        "exec",
                        self.container_name,
                        "mkdir",
                        "-p",
                        "/opt/tracefix/request-test",
                    ]
                )
                for name in ("service.py", "sitecustomize.py"):
                    source_path = tracefix_root / "docker" / "requests-test-tls" / name
                    if source_path.is_file():
                        _run(
                            [
                                self.docker,
                                "cp",
                                str(source_path),
                                f"{self.container_name}:/opt/tracefix/request-test/{name}",
                            ]
                        )
                with tempfile.TemporaryDirectory(prefix="tracefix-tls-") as tls_directory:
                    certificate = Path(tls_directory) / "test-ca.pem"
                    private_key = Path(tls_directory) / "test-key.pem"
                    write_test_tls_material(certificate, private_key)
                    tls_certificate_sha256 = hashlib.sha256(certificate.read_bytes()).hexdigest()
                    for path in (certificate, private_key):
                        _run(
                            [
                                self.docker,
                                "cp",
                                str(path),
                                f"{self.container_name}:/opt/tracefix/request-test/{path.name}",
                            ]
                        )
        sentinel = self.run_dir / "agent-only-sentinel.txt"
        sentinel.write_text(self.run_id, encoding="utf-8")
        _run(
            [
                self.docker,
                "exec",
                self.container_name,
                "tar",
                "-xf",
                "/input/source.tar",
                "-C",
                "/work/agent",
            ]
        )
        _run([self.docker, "exec", self.container_name, "git", "-C", "/work/agent", "init", "-q"])
        _run(
            [
                self.docker,
                "exec",
                self.container_name,
                "test",
                "!",
                "-e",
                "/work/agent/.tracefix-build-tmp",
            ]
        )
        _run([self.docker, "exec", self.container_name, "mkdir", "/work/agent/.tracefix-build-tmp"])
        self._copy_file_into(sentinel, "/work/agent/.tracefix-build-tmp/agent-only-sentinel")
        _run(
            [
                self.docker,
                "exec",
                self.container_name,
                "git",
                "-C",
                "/work/agent",
                "config",
                "user.email",
                "tracefix@invalid.local",
            ]
        )
        _run(
            [
                self.docker,
                "exec",
                self.container_name,
                "git",
                "-C",
                "/work/agent",
                "config",
                "user.name",
                "TraceFix",
            ]
        )
        _run([self.docker, "exec", self.container_name, "git", "-C", "/work/agent", "add", "-A"])
        _run(
            [
                self.docker,
                "exec",
                self.container_name,
                "git",
                "-C",
                "/work/agent",
                "commit",
                "-qm",
                source_commit,
            ]
        )
        _run(
            [
                self.docker,
                "exec",
                self.container_name,
                "git",
                "-C",
                "/work/agent",
                "tag",
                self.source_tag,
            ]
        )
        steps = []
        if recovery_enabled:
            if self.profile != "ordinary":
                raise WorkspaceError("Docker recovery requires the ordinary profile")
            # Private Git exclusions align generated caches with the snapshot product contract.
            # Tracked files remain visible to Git and are retained by the snapshot codec.
            _run(
                [
                    self.docker,
                    "exec",
                    self.container_name,
                    "sh",
                    "-c",
                    "printf '\\n__pycache__/\\n.pytest_cache/\\n' >> /work/agent/.git/info/exclude",
                ]
            )
        for template in self.recipe.get("build_commands", []):
            command = [self.python if arg == "{python}" else arg for arg in template]
            started = time.monotonic()
            result = _run(
                [self.docker, "exec", "-w", "/work/agent", self.container_name, *command],
                timeout=300,
            )
            steps.append(
                {
                    "command": command,
                    "returncode": result.returncode,
                    "duration_seconds": time.monotonic() - started,
                }
            )
        service_info = None
        if self.recipe.get("service_health_url"):
            service_python = "/opt/python39/bin/python3.9"
            process_log = "/work/httpbin.log"
            if self.recipe.get("test_pythonpath_entries"):
                launch = (
                    f"{service_python} /opt/tracefix/request-test/service.py "
                    "--certificate /opt/tracefix/request-test/test-ca.pem "
                    "--private-key /opt/tracefix/request-test/test-key.pem"
                )
            else:
                launch = (
                    f"{service_python} -m flask --app httpbin:app run --host 127.0.0.1 "
                    "--port 8765 --no-reload"
                )
            _run(
                [
                    self.docker,
                    "exec",
                    "-d",
                    self.container_name,
                    "sh",
                    "-c",
                    f"{launch} >{process_log} 2>&1",
                ]
            )
            healthy = False
            for _ in range(50):
                check = subprocess.run(
                    [
                        self.docker,
                        "exec",
                        self.container_name,
                        service_python,
                        "-c",
                        "import urllib.request; r=urllib.request.urlopen("
                        + repr(self.recipe["service_health_url"])
                        + ",timeout=1); assert r.status==200",
                    ],
                    capture_output=True,
                    check=False,
                )
                if check.returncode == 0:
                    healthy = True
                    break
                time.sleep(0.1)
            https_healthy = None
            if healthy and self.recipe.get("test_pythonpath_entries"):
                tls_check = subprocess.run(
                    [
                        self.docker,
                        "exec",
                        self.container_name,
                        service_python,
                        "-c",
                        "import ssl,urllib.request; c=ssl.create_default_context("
                        "cafile='/opt/tracefix/request-test/test-ca.pem'); "
                        "r=urllib.request.urlopen('https://127.0.0.1/get',context=c,timeout=2); "
                        "assert r.status==200",
                    ],
                    capture_output=True,
                    check=False,
                )
                https_healthy = tls_check.returncode == 0
            log_result = subprocess.run(
                [self.docker, "exec", self.container_name, "cat", process_log],
                capture_output=True,
                check=False,
            )
            service_info = {
                "healthy": healthy and https_healthy is not False,
                "http_healthy": healthy,
                "https_healthy": https_healthy,
                "tls_certificate_sha256": tls_certificate_sha256,
                "log": log_result.stdout.decode("utf-8", "replace")[-4000:],
            }
            if not service_info["healthy"]:
                raise WorkspaceError(
                    "local Requests test service did not become healthy", context=service_info
                )
        probe = self.recipe.get("source_import_probe")
        probe_data: dict[str, Any] = {"module": probe, "valid": None}
        if probe:
            script = (
                "import importlib,json,os; m=importlib.import_module("
                + repr(probe)
                + "); print(json.dumps(getattr(m,'__file__',None)))"
            )
            result = _run(
                [
                    self.docker,
                    "exec",
                    "-w",
                    "/work/agent",
                    self.container_name,
                    "env",
                    "PYTHONPATH=/work/agent/src:/work/agent",
                    self.python,
                    "-c",
                    script,
                ]
            )
            imported = json.loads(result.stdout.decode().strip())
            probe_data.update(
                {
                    "path": imported,
                    "valid": bool(
                        imported
                        and (imported == "/work/agent" or imported.startswith("/work/agent/"))
                    ),
                }
            )
            if not probe_data["valid"]:
                raise WorkspaceError(
                    "source import probe did not resolve inside Agent checkout", context=probe_data
                )
        environment = self.recipe.get("environment_variables", {})
        environment_path = "/opt/tracefix/runtime/environment.json"
        if self.profile != "ordinary":
            env_file = self.run_dir / "container-environment.json"
            env_file.write_text(json.dumps(environment, ensure_ascii=False), encoding="utf-8")
            self._copy_file_into(env_file, "/input/environment.json")
            environment_path = "/input/environment.json"
        self.artifact_environment_path = environment_path
        if recovery_snapshot is not None:
            self.restore_snapshot(recovery_snapshot)
        elif artifact_seed is not None:
            self.import_job_artifact_seed(artifact_seed)
        if skills_root is not None:
            if self.profile != "ordinary" or not skills_enabled:
                raise WorkspaceError("experience Skills require ordinary Docker with Skills")
            # Ordinary Skills were included in the root-owned trusted-executor bundle.
        command = [self.docker, "exec", "-i", self.container_name, "env"]
        if self.profile == "ordinary":
            command.extend(
                [
                    "-i",
                    "PATH=/usr/local/bin:/usr/bin:/bin",
                    "HOME=/tmp",
                    "PYTHONDONTWRITEBYTECODE=1",
                    "PYTHONPATH=/opt/tracefix/src",
                ]
            )
            python_flags = ["-P", "-s"]
        else:
            command.append("PYTHONPATH=/opt/tracefix/src")
            python_flags = []
        command.extend(
            [
                self.python,
                *python_flags,
                "-m",
                "tracefix.agent_bridge",
                "--workspace",
                "/work/agent",
                "--evidence",
                "/work/evidence",
                "--python",
                self.python,
                "--timeout",
                str(self.timeout_seconds),
                "--run-id",
                self.run_id,
                "--execution-profile",
                self.execution_profile.value,
                "--environment-json",
                environment_path,
            ]
        )
        if self.profile == "ordinary":
            command.extend(
                [
                    "--capability-policy-file",
                    "/opt/tracefix/runtime/capability-policy.json",
                ]
            )
        if self.recipe.get("pytest_config"):
            command.extend(["--pytest-config", self.recipe["pytest_config"]])
        for path in self.recipe.get("test_pythonpath_entries", []):
            command.extend(["--pythonpath", path])
        if repo_map_task is not None and repo_map_config is not None and repo_map_config.enabled:
            command.extend(
                [
                    "--repo-map-task",
                    repo_map_task,
                    "--repo-map-config",
                    repo_map_config.model_dump_json(),
                ]
            )
        if skills_enabled:
            command.extend(
                [
                    "--skills-enabled",
                    "--skill-limits-json",
                    (skill_limits or SkillLimits()).model_dump_json(),
                ]
            )
            if skills_root is not None or skills_snapshot is not None:
                command.extend(["--skills-root", "/opt/tracefix/runtime/skills"])
        if artifacts_enabled:
            command.extend(
                [
                    "--artifacts-enabled",
                    "--artifact-root",
                    "/work/artifact-evidence/tool-artifacts",
                ]
            )
        if self.background_jobs_enabled:
            command.extend(["--jobs-context-file", "/opt/tracefix/runtime/job-context.json"])
        if self.repository_rules_enabled:
            command.append("--repository-rules-enabled")
        self.session = _BridgeSession(
            command,
            self.timeout_seconds + 60,
            self.run_dir / "tool-events.jsonl",
            skills_enabled=skills_enabled,
            artifacts_enabled=artifacts_enabled,
            execution_profile=self.execution_profile,
            executor_policy_config_sha256=self.executor_policy_config_sha256,
            **({"job_protocol": self.job_protocol} if self.background_jobs_enabled else {}),
            **({"repository_rules_enabled": True} if self.repository_rules_enabled else {}),
        )
        if self.session.run_id != self.run_id:
            raise WorkspaceError("Docker bridge run identity mismatch")
        if recovery_snapshot is not None:
            from tracefix.docker_recovery import tool_identity

            tool_sha = tool_identity(self.session.tools)
            if tool_sha != recovery_snapshot.get("tool_sha256"):
                raise WorkspaceError("Docker recovery tool definitions changed")
            if (
                recovery_snapshot.get("capability_catalog_sha256")
                != self.session.capability_catalog_sha256
                or recovery_snapshot.get("capability_policy_sha256")
                != self.session.capability_policy_sha256
            ):
                raise WorkspaceError("Docker recovery capability policy changed")
            if container_interpreter != recovery_snapshot.get("interpreter"):
                raise WorkspaceError("Docker recovery interpreter identity changed")
            restored = self.session.call("__restore_state__", recovery_snapshot["bridge_state"])
            if not restored.success or restored.output != recovery_snapshot["bridge_state"]:
                raise WorkspaceError("Docker bridge state did not restore exactly")
        self.repo_map = self.session.repo_map
        self.workspace_preparation = {
            "success": True,
            "backend": "docker",
            "profile": self.profile,
            "security_policy_version": SECURITY_POLICY_VERSION,
            "trusted_executor_sha256": self.executor_bundle_sha256,
            "executor_policy_config_sha256": self.executor_policy_config_sha256,
            "skills_bundle_sha256": self.skills_bundle_sha256,
            "executor_write_probe": self.executor_write_probe,
            "image_id": actual_id,
            "container_id": self.container_id,
            "container_network_mode": network_mode,
            "container_mounts": mounts,
            "container_user": container_user,
            "container_limits": {
                "pids": host_config.get("PidsLimit"),
                "memory_bytes": host_config.get("Memory"),
                "nano_cpus": host_config.get("NanoCpus"),
                "read_only_root": host_config.get("ReadonlyRootfs"),
                "tmpfs": host_config.get("Tmpfs"),
            }
            if host_config
            else None,
            "source_commit": source_commit,
            "source_version_tag": self.source_tag,
            "agent_only_sentinel_sha256": hashlib.sha256(sentinel.read_bytes()).hexdigest(),
            "recipe_fingerprint": self._recipe_fingerprint()
            if self.profile != "ordinary"
            else None,
            "test_target": self.recipe.get("test_target"),
            "build_steps": steps,
            "source_import_probe": probe_data,
            "container_interpreter": container_interpreter,
            "local_service": service_info,
            "protocol": 1,
        }
        self._write_run_state("agent_ready")
        return ToolRegistry(
            _RemoteTool(spec, self, self.session.skill_catalog) for spec in self.session.tools
        )

    def remove_previous_container(self, snapshot: dict[str, Any]) -> None:
        """Remove an abandoned container only after checking its exact owned identity."""
        previous = snapshot.get("container_id")
        if (
            not isinstance(previous, str)
            or re.fullmatch(r"[0-9a-f]{64}", previous) is None
            or previous == self.container_id
        ):
            raise WorkspaceError("invalid previous Docker container identity")
        found = _run(
            [
                self.docker,
                "ps",
                "-a",
                "--no-trunc",
                "--filter",
                f"id={previous}",
                "--format",
                "{{.ID}}",
            ]
        )
        identifiers = found.stdout.decode("utf-8").splitlines()
        if not identifiers:
            return
        if identifiers != [previous]:
            raise WorkspaceError("previous Docker container identity is ambiguous")
        owned = _run(
            [
                self.docker,
                "inspect",
                "--format",
                '{{index .Config.Labels "tracefix.run_id"}}',
                previous,
            ]
        )
        if owned.stdout.decode("utf-8").strip() != self.run_id:
            raise WorkspaceError("previous Docker container ownership changed; removal refused")
        _run([self.docker, "rm", "-f", previous])

    def save_snapshot(self, sequence: int) -> dict[str, Any]:
        """Stream bounded product data and bind its verified bridge and journal state."""
        from tracefix.docker_recovery import tool_identity, validate_artifact_archive
        from tracefix.memory import atomic_json, digest
        from tracefix.workspace_snapshot import validate_archive

        if self.profile != "ordinary" or self.session is None:
            raise WorkspaceError("only an active ordinary Docker bridge can save a snapshot")
        decision = self._control_decision(
            "checkpoint_commit", CallerOrigin.HOST_LIFECYCLE
        )
        if not decision.allowed:
            raise WorkspaceError(
                "Docker checkpoint commit denied by capability policy",
                context={"reason_code": decision.reason_code},
            )
        state = self.session.call("__recovery_state__", {})
        if not state.success or not isinstance(state.output, dict):
            raise WorkspaceError("could not save Docker bridge state")
        directory = self.run_dir / "docker-checkpoints"
        directory.mkdir(exist_ok=True)
        identity = f"{sequence:08d}-{uuid4().hex}"
        archive_path = directory / f"{identity}.tar"
        temporary = directory / f"{identity}.partial"
        artifact_archive_path = directory / f"{identity}.artifacts.tar"
        artifact_temporary = directory / f"{identity}.artifacts.partial"
        artifact_record: dict[str, Any] = {}
        artifact_published = False
        jobs_enabled = getattr(self, "background_jobs_enabled", False)
        artifact_run_id = self.job_settings.logical_run_id if jobs_enabled else self.run_id
        try:
            if self.artifacts_enabled:
                if self.artifact_environment_path is None:
                    raise WorkspaceError("artifact bridge environment identity is unavailable")
                with artifact_temporary.open("xb") as output:
                    transfer = subprocess.run(
                        [
                            self.docker,
                            "exec",
                            self.container_name,
                            "env",
                            "PYTHONPATH=/opt/tracefix/src",
                            self.python,
                            "-m",
                            "tracefix.artifact_transport",
                            "export",
                            "--root",
                            "/work/artifact-evidence/tool-artifacts",
                            "--workspace",
                            "/work/agent",
                            "--environment-json",
                            self.artifact_environment_path,
                            "--run-id",
                            artifact_run_id,
                            *(["--streaming-enabled"] if jobs_enabled else []),
                        ],
                        stdout=output,
                        stderr=subprocess.PIPE,
                        timeout=self.timeout_seconds + 120,
                        check=False,
                    )
                    output.flush()
                    os.fsync(output.fileno())
                if transfer.returncode:
                    raise WorkspaceError(
                        "Docker artifact export failed",
                        context={"stderr": transfer.stderr.decode("utf-8", "replace")[-2000:]},
                    )
                try:
                    receipt = json.loads(transfer.stderr.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise WorkspaceError("Docker artifact export receipt is invalid") from exc
                if (
                    not isinstance(receipt, dict)
                    or receipt.get("run_id") != artifact_run_id
                    or receipt.get("schema_version") != 1
                    or artifact_temporary.stat().st_size > 68 * 1024 * 1024
                ):
                    raise WorkspaceError("Docker artifact export exceeded its contract")
                os.replace(artifact_temporary, artifact_archive_path)
                artifact_published = True
                with artifact_archive_path.open("rb") as artifact_stream:
                    artifact_archive_sha256 = hashlib.file_digest(
                        artifact_stream, "sha256"
                    ).hexdigest()
                artifact_record = {
                    "artifacts_enabled": True,
                    "run_id": self.run_id,
                    "artifact_archive": artifact_archive_path.relative_to(self.run_dir).as_posix(),
                    "artifact_archive_bytes": artifact_archive_path.stat().st_size,
                    "artifact_archive_sha256": artifact_archive_sha256,
                    "artifact_receipt": receipt,
                    **({"streaming_artifacts": True, "artifact_run_id": artifact_run_id,
                        "job_protocol": self.job_protocol} if jobs_enabled else {}),
                }
            with temporary.open("xb") as output:
                result = subprocess.run(
                    [
                        self.docker,
                        "exec",
                        self.container_name,
                        "env",
                        "PYTHONPATH=/opt/tracefix/src",
                        self.python,
                        "-m",
                        "tracefix.workspace_snapshot",
                        "export",
                        "--workspace",
                        "/work/agent",
                    ],
                    stdout=output,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout_seconds + 120,
                    check=False,
                )
                output.flush()
                os.fsync(output.fileno())
            if result.returncode:
                raise WorkspaceError(
                    "Docker product snapshot failed",
                    context={
                        "stderr": result.stderr.decode("utf-8", "replace")[-2000:],
                    },
                )
            metadata = json.loads(result.stderr.decode("utf-8"))
            validate_archive(temporary, metadata)
            os.replace(temporary, archive_path)
            self.export_evidence()
            journal = (self.run_dir / "tool-events.jsonl").read_bytes()
            record = {
                "schema_version": 1,
                "sequence": sequence,
                "batch_id": identity,
                "archive": archive_path.relative_to(self.run_dir).as_posix(),
                "metadata": metadata,
                "bridge_state": state.output,
                "bridge_state_sha256": digest(state.output),
                "journal_size": len(journal),
                "journal_sha256": hashlib.sha256(journal).hexdigest(),
                "baseline_sha256": hashlib.sha256(
                    (self.run_dir / "agent-base.tar").read_bytes(),
                ).hexdigest(),
                "image_id": self.image_id,
                "container_id": self.container_id,
                "interpreter": self.workspace_preparation["container_interpreter"],
                "tool_sha256": tool_identity(self.session.tools),
                "capability_catalog_sha256": self.session.capability_catalog_sha256,
                "capability_policy_sha256": self.session.capability_policy_sha256,
                "executor_policy_config_sha256": self.executor_policy_config_sha256,
                "host_control_policy_sha256": self._host_control_policy.sha256,
                **({"repository_rules_protocol_version": 1}
                   if getattr(self, "repository_rules_enabled", False) else {}),
                "host_control_catalog_sha256": self._host_control_policy.catalog.sha256,
                "checkpoint_commit_decision": decision.model_dump(mode="json"),
                "test_evidence": {
                    path.relative_to(self.run_dir / "test-evidence").as_posix():
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in ((self.run_dir / "test-evidence").rglob("*") if jobs_enabled
                                 else (self.run_dir / "test-evidence").iterdir())
                    if path.is_file()
                },
                **artifact_record,
            }
            if self.artifacts_enabled:
                validate_artifact_archive(
                    self.run_dir,
                    record,
                    expected_run_id=artifact_run_id,
                    streaming_enabled=jobs_enabled,
                )
            else:
                record["artifacts_enabled"] = False
            atomic_json(directory / f"{identity}.json", record)
            return record
        finally:
            temporary.unlink(missing_ok=True)
            artifact_temporary.unlink(missing_ok=True)
            if artifact_published and not (directory / f"{identity}.json").is_file():
                artifact_archive_path.unlink(missing_ok=True)

    def source_snapshot_provider(self):
        """Return a filtered exporter command without exposing the recovery archive."""
        if self.profile != "ordinary" or self.container_id is None:
            raise WorkspaceError("source export requires a prepared ordinary Docker workspace")
        from tracefix.source_snapshot import CommandSourceSnapshotProvider

        return CommandSourceSnapshotProvider(
            (
                self.docker,
                "exec",
                self.container_id,
                "env",
                "-i",
                "PATH=/usr/local/bin:/usr/bin:/bin",
                "HOME=/tmp",
                "PYTHONPATH=/opt/tracefix/src",
                self.python,
                "-P",
                "-s",
                "-m",
                "tracefix.source_snapshot",
                "--export",
                "/work/agent",
            ),
            timeout_seconds=45,
            **({"authorize": self._authorize_source_export}
               if getattr(self, "background_jobs_enabled", False) else {}),
        )

    def restore_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Restore a validated archive before starting the new container's bridge."""
        from tracefix.memory import atomic_json
        from tracefix.workspace_snapshot import validate_archive

        jobs_enabled = getattr(self, "background_jobs_enabled", False)
        artifact_run_id = self.job_settings.logical_run_id if jobs_enabled else self.run_id
        if jobs_enabled:
            from tracefix.jobs.protocol import JobProtocolHello

            prior = JobProtocolHello.model_validate(snapshot.get("job_protocol"))
            if prior.logical_run_id != artifact_run_id:
                raise WorkspaceError("Docker job recovery logical identity changed")
        elif "job_protocol" in snapshot:
            raise WorkspaceError("Docker job recovery requires its original execution mode")
        rules_version = snapshot.get("repository_rules_protocol_version")
        rules_enabled = getattr(self, "repository_rules_enabled", False)
        rules_match = (type(rules_version) is int and rules_version == 1
                       if rules_enabled else rules_version is None)
        if not rules_match:
            raise WorkspaceError("Docker rule recovery requires its original protocol and mode")

        decision = self._control_decision(
            "checkpoint_restore", CallerOrigin.RECOVERY
        )
        if not decision.allowed:
            raise WorkspaceError(
                "Docker checkpoint restore denied by capability policy",
                context={"reason_code": decision.reason_code},
            )
        if (
            snapshot.get("host_control_policy_sha256") != self._host_control_policy.sha256
            or snapshot.get("host_control_catalog_sha256")
            != self._host_control_policy.catalog.sha256
        ):
            raise WorkspaceError("Docker checkpoint control policy identity changed")
        if snapshot.get("executor_policy_config_sha256") != self.executor_policy_config_sha256:
            raise WorkspaceError("Docker checkpoint executor policy bundle changed")
        if self.artifacts_enabled != (snapshot.get("artifacts_enabled") is True):
            raise WorkspaceError("Docker artifact capability differs from its recovery snapshot")

        path = self.run_dir / snapshot["archive"]
        validate_archive(path, snapshot["metadata"])
        metadata_path = self.run_dir / "restore-metadata.json"
        atomic_json(metadata_path, snapshot["metadata"])
        self._copy_file_into(metadata_path, "/input/restore-metadata.json")
        # Streaming stdin avoids a second 1 GiB host buffer and preserves container tmpfs space.
        with path.open("rb") as source:
            restored = subprocess.run(
                [
                    self.docker,
                    "exec",
                    "-i",
                    self.container_name,
                    "env",
                    "PYTHONPATH=/opt/tracefix/src",
                    self.python,
                    "-m",
                    "tracefix.workspace_snapshot",
                    "restore-stream",
                    "--workspace",
                    "/work/agent",
                    "--metadata",
                    "/input/restore-metadata.json",
                ],
                stdin=source,
                capture_output=True,
                timeout=self.timeout_seconds + 120,
                check=False,
            )
        if restored.returncode:
            raise WorkspaceError(
                "could not restore Docker recovery archive",
                context={
                    "stderr": restored.stderr.decode("utf-8", "replace")[-2000:],
                },
            )
        _run([self.docker, "exec", self.container_name, "rm", "-f", "/input/restore-metadata.json"])
        self._restore_artifacts(snapshot, snapshot_root=self.run_dir)

    def import_job_artifact_seed(self, seed: JobArtifactSeed) -> None:
        """A fresh executor imports the previous owner's entire quota, never a subset."""
        if not self.background_jobs_enabled or not self.artifacts_enabled:
            raise WorkspaceError("artifact seeding requires background job artifacts")
        decision = self._control_decision("checkpoint_restore", CallerOrigin.RECOVERY)
        if not decision.allowed:
            raise WorkspaceError("artifact seeding denied", context={
                "reason_code": decision.reason_code})
        seed = JobArtifactSeed.model_validate_json(seed.model_dump_json())
        self._restore_artifacts(seed.snapshot, snapshot_root=seed.root)

    def _restore_artifacts(self, snapshot: dict, *, snapshot_root: Path) -> None:
        from tracefix.docker_recovery import owned_file, validate_artifact_archive

        jobs_enabled = getattr(self, "background_jobs_enabled", False)
        artifact_run_id = self.job_settings.logical_run_id if jobs_enabled else self.run_id
        if self.artifacts_enabled:
            if self.artifact_environment_path is None:
                raise WorkspaceError("artifact restore environment identity is unavailable")
            receipt = validate_artifact_archive(
                snapshot_root,
                snapshot,
                expected_run_id=artifact_run_id,
                streaming_enabled=jobs_enabled,
            )
            artifact_archive = owned_file(snapshot_root, snapshot["artifact_archive"])
            with artifact_archive.open("rb") as source:
                restored_artifacts = subprocess.run(
                    [
                        self.docker,
                        "exec",
                        "-i",
                        self.container_name,
                        "env",
                        "PYTHONPATH=/opt/tracefix/src",
                        self.python,
                        "-m",
                        "tracefix.artifact_transport",
                        "import",
                        "--root",
                        "/work/artifact-evidence/tool-artifacts",
                        "--workspace",
                        "/work/agent",
                        "--environment-json",
                        self.artifact_environment_path,
                        "--run-id",
                        artifact_run_id,
                        *(["--streaming-enabled"] if jobs_enabled else []),
                    ],
                    stdin=source,
                    capture_output=True,
                    timeout=self.timeout_seconds + 120,
                    check=False,
                )
            if restored_artifacts.returncode:
                raise WorkspaceError(
                    "could not restore Docker tool artifacts",
                    context={
                        "stderr": restored_artifacts.stderr.decode("utf-8", "replace")[-2000:]
                    },
                )
            try:
                imported = json.loads(restored_artifacts.stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkspaceError("Docker artifact restore receipt is invalid") from exc
            for field in (
                "manifest_sha256",
                "artifact_count",
                "artifact_ids",
                "storage_bytes",
                "queries",
                "returned_chars",
            ):
                if imported.get(field) != receipt.get(field):
                    raise WorkspaceError("Docker restored artifact quota disagrees with checkpoint")

    def set_phase(self, phase: str) -> None:
        """Persist the owned container identity and current lifecycle phase."""
        self._write_run_state(phase)

    def _write_run_state(self, phase: str) -> None:
        state = {
            "schema_version": 1,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "profile": self.profile,
            "phase": phase,
            "container_id": self.container_id,
            "container_name": self.container_name,
            "input_root": str(self.input_root),
            "image_id": getattr(self, "image_id", None),
            "source_commit": self.source_commit,
            "recipe_fingerprint": self._recipe_fingerprint()
            if (self.input_root / self.task_id / "recipes" / f"{self.task_id}.json").is_file()
            else None,
            "input_manifest_sha256": self.input_manifest_sha256,
            "trusted_executor_sha256": self.executor_bundle_sha256,
            "executor_write_probe": self.executor_write_probe,
        }
        path = self.run_dir / "docker-run-state.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def _source_archive(self, source_repo: Path, commit: str) -> Path:
        temporary = self.run_dir / "agent-base.tar"
        # Windows Git may rewrite blob LF to CRLF while exporting a tarball. The
        # container checkout must retain the frozen Git blob bytes exactly.
        archive = _run(
            [
                "git",
                "-c",
                "core.autocrlf=false",
                "-C",
                str(source_repo),
                "archive",
                "--format=tar",
                commit,
            ]
        )
        temporary.write_bytes(archive.stdout)
        with tarfile.open(temporary, "r") as archive_file:
            members = archive_file.getmembers()
            if any(
                member.name.split("/", 1)[0] in {".tracefix-build-tmp", ".tracefix-test-tmp"}
                for member in members
            ):
                raise WorkspaceError(
                    "frozen source conflicts with reserved TraceFix evidence paths"
                )
        return temporary

    def _capture_job_context(self) -> None:
        """Capture only known startup roles, before importing any project code."""
        from tracefix.jobs.protocol import JobWorkerContext

        assert self.job_settings is not None
        helper = Path(__file__).resolve().parent / "jobs" / "docker_context.py"
        if (helper.is_symlink() or helper.parent.is_symlink() or not helper.is_file()
                or helper.stat().st_size > 16 * 1024):
            raise WorkspaceError("trusted Docker job bootstrap is unavailable")
        captured = _run([
            self.docker, "exec", self.container_name, self.python, "-I", "-S", "-c",
            helper.read_text(encoding="utf-8"),
        ])
        try:
            payload = json.loads(captured.stdout.decode("utf-8"))
            if set(payload) != {"contract", "tokens"}:
                raise ValueError("unexpected bootstrap fields")
            self.job_context = JobWorkerContext(
                **self.job_settings.model_dump(), contract=payload["contract"],
                physical_run_id=self.run_id, fixed_control_tokens=payload["tokens"])
        except (ValueError, KeyError, TypeError) as exc:
            raise WorkspaceError("Docker job control identity capture failed") from exc

    def _install_job_context(self) -> None:
        """Install fixed ownership data as root, never in the task workspace."""
        from tracefix.jobs.protocol import JobProtocolHello

        assert self.job_context is not None
        raw = json.dumps(self.job_context.model_dump(mode="json"), sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
        program = (
            "import os,stat,sys\n"
            "directory='/opt/tracefix/runtime'\n"
            "s=os.lstat(directory)\n"
            "if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or "
            "stat.S_IMODE(s.st_mode)!=0o555: raise RuntimeError('invalid runtime directory')\n"
            "os.chmod(directory,0o755)\n"
            "try:\n"
            "    fd=os.open(directory+'/job-context.json',"
            "os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o444)\n"
            "    with os.fdopen(fd,'wb') as f:\n"
            "        f.write(sys.argv[1].encode('utf-8')); f.flush(); os.fsync(f.fileno())\n"
            "finally:\n"
            "    os.chmod(directory,0o555)\n"
        )
        _run([self.docker, "exec", "--user", "0:0", self.container_name, self.python,
              "-I", "-S", "-c", program, raw])
        self.job_protocol = JobProtocolHello(
            version=1, worker_epoch=self.job_context.worker_epoch,
            logical_run_id=self.job_context.logical_run_id,
            host_owner_epoch=self.job_context.host_owner_epoch,
            context_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        ).model_dump(mode="json")

    def _copy_file_into(self, source: Path, destination: str) -> None:
        if self.profile == "ordinary":
            _run(
                [
                    self.docker,
                    "exec",
                    "-i",
                    self.container_name,
                    "sh",
                    "-c",
                    f"cat > {destination}",
                ],
                input_data=source.read_bytes(),
                timeout=120,
            )
        else:
            _run([self.docker, "cp", str(source), f"{self.container_name}:{destination}"])

    def _install_trusted_executor(self, bundle: bytes, expected_sha256: str) -> None:
        """Install the trusted bridge as root before any task code is executed."""
        root = [self.docker, "exec", "--user", "0:0", self.container_name]
        _run(
            [
                *root,
                "mkdir",
                "-p",
                "/opt/tracefix/src",
                "/opt/tracefix/runtime",
                "/opt/tracefix/request-test",
            ]
        )
        _run(
            [
                *root[:-1],
                "-i",
                self.container_name,
                "tar",
                "-xf",
                "-",
                "--no-same-owner",
                "-C",
                "/opt/tracefix",
            ],
            input_data=bundle,
            timeout=120,
        )
        _run([*root, "find", "/opt/tracefix", "-type", "f", "-exec", "chmod", "0444", "{}", "+"])
        _run([*root, "find", "/opt/tracefix", "-type", "d", "-exec", "chmod", "0555", "{}", "+"])
        verify = (
            "import hashlib,json,pathlib,stat,sys; "
            "root=pathlib.Path('/opt/tracefix'); "
            "manifest=json.loads((root/'runtime/executor-manifest.json').read_text()); "
            "files=manifest['files']; "
            "packed=json.dumps(files,sort_keys=True,separators=(',',':')).encode(); "
            "expected=sys.argv[1]; "
            "hashlib.sha256(packed).hexdigest()==expected and "
            "manifest.get('bundle_sha256')==expected or sys.exit(10); "
            "[(lambda p,d: (p.relative_to(root), "
            "hashlib.sha256(p.read_bytes()).hexdigest()==d or sys.exit(11)))"
            "((root/name).resolve(strict=True), digest) "
            "for name,digest in files.items()]; "
            "[(lambda p,s: (s.st_uid==0 and "
            "stat.S_IMODE(s.st_mode)==(0o555 if p.is_dir() else 0o444)) or sys.exit(12))"
            "(p,p.lstat()) for p in (root,*root.rglob('*'))]; "
            "print(expected)"
        )
        checked = _run(
            [
                self.docker,
                "exec",
                self.container_name,
                "env",
                "-i",
                "PATH=/usr/local/bin:/usr/bin:/bin",
                "HOME=/tmp",
                self.python,
                "-P",
                "-s",
                "-S",
                "-c",
                verify,
                expected_sha256,
            ]
        )
        if checked.stdout.decode("utf-8", "replace").strip() != expected_sha256:
            raise WorkspaceError("trusted executor identity verification failed")
        write_probe = (
            "import pathlib\n"
            "p = pathlib.Path('/opt/tracefix/.write-probe')\n"
            "try:\n"
            "    p.write_text('probe')\n"
            "except OSError:\n"
            "    print('blocked')\n"
            "else:\n"
            "    p.unlink(missing_ok=True)\n"
            "    raise SystemExit(12)\n"
        )
        probe = _run(
            [
                self.docker,
                "exec",
                "--user",
                "10001:10001",
                self.container_name,
                "env",
                "-i",
                "PATH=/usr/local/bin:/usr/bin:/bin",
                "HOME=/tmp",
                self.python,
                "-P",
                "-s",
                "-S",
                "-c",
                write_probe,
            ]
        )
        if probe.stdout.decode("utf-8", "replace").strip() != "blocked":
            raise WorkspaceError("task UID can modify the trusted executor directory")
        self.executor_write_probe = "blocked"
        self.executor_bundle_sha256 = expected_sha256

    def _check_storage(self) -> None:
        if os.name == "nt":
            vhdx = Path(r"E:\dockerspace\DockerDesktopWSL\disk\docker_data.vhdx")
            if not vhdx.exists():
                raise WorkspaceError("cannot verify Docker Desktop storage volume")
            free = shutil.disk_usage(vhdx.anchor).free
        else:
            free = shutil.disk_usage("/").free
        if free < 10 * 1024**3:
            raise WorkspaceError(
                "Docker storage volume has less than 10 GiB free", context={"free_bytes": free}
            )

    def _recipe_fingerprint(self) -> str:
        path = self.input_root / self.task_id / "recipes" / f"{self.task_id}.json"
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def export_evidence(self) -> None:
        if not self.container_id:
            return
        if getattr(self, "background_jobs_enabled", False):
            self._export_job_evidence()
            return
        target = self.run_dir / "test-evidence"
        target.mkdir(exist_ok=True)
        completed = subprocess.run(
            [self.docker, "cp", f"{self.container_id}:/work/evidence/.", str(target)],
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            detail = completed.stderr.decode("utf-8", "replace")[-2000:]
            raise WorkspaceError(
                "could not export container test evidence", context={"stderr": detail}
            )
        if self.recipe.get("service_health_url"):
            service_log = subprocess.run(
                [self.docker, "exec", self.container_id, "cat", "/work/httpbin.log"],
                capture_output=True,
                check=False,
            )
            if service_log.returncode == 0:
                (self.run_dir / "httpbin-service.log").write_bytes(service_log.stdout)

    def _export_job_evidence(self) -> None:
        """Read tmpfs through the owned container, with a validated, bounded manifest."""
        from tracefix.jobs.evidence import validate

        if self.session is None:
            raise WorkspaceError("job evidence export requires a live verified bridge")
        state = self.session.call("__recovery_state__", {})
        if not state.success:
            raise WorkspaceError("job evidence cannot export active or unknown executions")
        completed = _run([
            self.docker, "exec", self.container_id, "env", "-i",
            "PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/tmp",
            self.python, "-I", "-S", "/opt/tracefix/src/tracefix/jobs/evidence.py",
            "/work/evidence",
        ])
        try:
            files = validate(completed.stdout)
        except (ValueError, TypeError, KeyError) as exc:
            raise WorkspaceError("job audit evidence transfer failed validation") from exc
        target = self.run_dir / "test-evidence"
        # Never reuse an extraction directory that project-controlled data could poison.
        with tempfile.TemporaryDirectory(prefix="job-audit-", dir=self.run_dir) as temporary:
            staging = Path(temporary) / "files"
            staging.mkdir()
            for name, data in files.items():
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            if target.exists() or target.is_symlink():
                if (target.is_symlink() or not target.is_dir()
                        or getattr(target.lstat(), "st_file_attributes", 0) & 0x400):
                    raise WorkspaceError("job audit evidence destination is unsafe")
                # Snapshots retain the previous copy until a fully verified replacement exists.
                previous = Path(temporary) / "previous"
                os.replace(target, previous)
                try:
                    os.replace(staging, target)
                except BaseException:
                    os.replace(previous, target)
                    raise
            else:
                os.replace(staging, target)

    def close(self, *, remove: bool = True) -> None:
        errors: list[str] = []
        if self.session is not None:
            try:
                self.session.close()
            except Exception as exc:
                errors.append(f"bridge close: {exc}")
            self.session = None
        if remove and self.container_id and not getattr(self, "_container_removed", False):
            try:
                inspected = _run(
                    [
                        self.docker,
                        "inspect",
                        "--format",
                        '{{index .Config.Labels "tracefix.run_id"}}',
                        self.container_id,
                    ]
                )
                if inspected.stdout.decode().strip() == self.run_id:
                    _run([self.docker, "rm", "-f", self.container_id])
                    self._container_removed = True
                else:
                    errors.append("container ownership label changed; removal refused")
            except Exception as exc:
                errors.append(f"container removal: {exc}")
        if errors and self.profile == "ordinary":
            raise WorkspaceError(
                "ordinary Docker cleanup was incomplete",
                context={"container_id": self.container_id, "errors": errors},
            )
