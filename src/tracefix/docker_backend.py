"""Linux Docker workspace and RPC-backed repository tools.

This backend deliberately accepts only the three frozen, offline task recipes.
It never mounts the host repository or forwards the host environment to Docker.
"""

from __future__ import annotations

import hashlib
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

from tracefix.docker_tls import write_test_tls_material
from tracefix.exceptions import RunConfigurationError, WorkspaceError
from tracefix.messages import ToolCall
from tracefix.repository import RepoMap, RepoMapConfig
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
    ) -> None:
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
        load_specs = [spec for spec in specs if spec.name == "load_skill"]
        if skills_enabled:
            declared = (
                set(
                    load_specs[0]
                    .input_schema.get("properties", {})
                    .get("name", {})
                    .get("enum", [])
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
        self, spec: ToolSpec, backend: DockerToolBackend,
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
    """Own one task container and expose the existing five tools over JSONL."""

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
        self.requested_image_id = image_id
        self.task_id = task_id
        self.input_root = input_root.expanduser().resolve(strict=True)
        self.run_dir = run_dir
        self.run_id = run_id
        self.docker = os.environ.get("DOCKER", "docker")
        self.container_name = f"tracefix-{run_id.lower().replace('_', '-')[:40]}"
        self.container_id: str | None = None
        self.session: _BridgeSession | None = None
        self.timeout_seconds = timeout_seconds
        self.recipe: dict[str, Any] = {}
        self.workspace_preparation: dict[str, Any] = {}
        self.source_commit: str | None = None
        self.input_manifest_sha256: str | None = None

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
    ) -> ToolRegistry:
        self.source_commit = source_commit
        if self.profile == "ordinary":
            if not source_import_probe:
                raise WorkspaceError("ordinary Docker requires a source import probe")
            self.recipe = {"source_import_probe": source_import_probe}
            manifest = {}
        else:
            stage = self.input_root / self.task_id
            bundle = stage / "source.bundle"
            recipe_path = stage / "recipes" / f"{self.task_id}.json"
            manifest_path = stage / "input-manifest.json"
            if not bundle.is_file() or not recipe_path.is_file() or not manifest_path.is_file():
                raise WorkspaceError("frozen Docker task input is incomplete")
            self.recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
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
        source = self._source_archive(source_repo, source_commit)
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
                        "--cpus", "1", "--read-only", "--user", "10001:10001",
                        "--tmpfs", "/tmp:rw,nosuid,size=512m,uid=10001,gid=10001",
                        "--tmpfs", "/input:rw,nosuid,size=256m,uid=10001,gid=10001",
                        "--tmpfs", "/work:rw,nosuid,size=1g,uid=10001,gid=10001",
                        "--tmpfs", "/opt/tracefix:rw,nosuid,size=128m,uid=10001,gid=10001",
                    ] if self.profile == "ordinary" else []
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
                self.image_id,
                "sleep",
                "infinity",
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
            host_config = json.loads(
                _run(
                    [self.docker, "inspect", "--format", "{{json .HostConfig}}", self.container_id]
                ).stdout.decode("utf-8", "strict")
            ) if self.profile == "ordinary" else None
            container_user = (
                _run(
                    [self.docker, "inspect", "--format", "{{.Config.User}}", self.container_id]
                ).stdout.decode("utf-8", "strict").strip()
                if self.profile == "ordinary" else None
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
        self._write_run_state("container_created")
        _run([self.docker, "start", self.container_name])
        container_interpreter: dict[str, str] | None = None
        if self.profile == "ordinary":
            probe_result = _run([
                self.docker, "exec", self.container_name, self.python,
                "-c", "import json,platform,pytest; print(json.dumps({"
                "'python':platform.python_version(),'pytest':pytest.__version__}))",
            ])
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
                "/opt/tracefix",
                "/work/evidence",
            ]
        )
        _run([self.docker, "cp", str(source), f"{self.container_name}:/input/source.tar"])
        _run([self.docker, "exec", self.container_name, "mkdir", "-p", "/opt/tracefix/src"])
        package_root = Path(__file__).resolve().parent
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
        _run(
            [
                self.docker,
                "cp",
                str(sentinel),
                f"{self.container_name}:/work/agent/.tracefix-build-tmp/agent-only-sentinel",
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
        env_file = self.run_dir / "container-environment.json"
        env_file.write_text(json.dumps(environment, ensure_ascii=False), encoding="utf-8")
        _run([self.docker, "cp", str(env_file), f"{self.container_name}:/input/environment.json"])
        command = [
            self.docker,
            "exec",
            "-i",
            self.container_name,
            "env",
            "PYTHONPATH=/opt/tracefix/src:/work/agent/src:/work/agent",
            self.python,
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
            "--environment-json",
            "/input/environment.json",
        ]
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
        self.session = _BridgeSession(
            command, self.timeout_seconds + 60, self.run_dir / "tool-events.jsonl",
            skills_enabled=skills_enabled,
        )
        if self.session.run_id != self.run_id:
            raise WorkspaceError("Docker bridge run identity mismatch")
        self.repo_map = self.session.repo_map
        self.workspace_preparation = {
            "success": True,
            "backend": "docker",
            "profile": self.profile,
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
            } if host_config else None,
            "source_commit": source_commit,
            "source_version_tag": self.source_tag,
            "agent_only_sentinel_sha256": hashlib.sha256(sentinel.read_bytes()).hexdigest(),
            "recipe_fingerprint": self._recipe_fingerprint()
            if self.profile != "ordinary" else None,
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

    def close(self, *, remove: bool = True) -> None:
        errors: list[str] = []
        if self.session is not None:
            try:
                self.session.close()
            except Exception as exc:
                errors.append(f"bridge close: {exc}")
            self.session = None
        if remove and self.container_id:
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
                else:
                    errors.append("container ownership label changed; removal refused")
            except Exception as exc:
                errors.append(f"container removal: {exc}")
        if errors and self.profile == "ordinary":
            raise WorkspaceError(
                "ordinary Docker cleanup was incomplete",
                context={"container_id": self.container_id, "errors": errors},
            )
