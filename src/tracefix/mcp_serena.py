"""Optional, isolated read-only Serena tools over the official MCP stdio protocol.

Each call gets a fresh source snapshot and container. The short-lived session costs
startup time, but makes patch freshness and process cleanup explicit.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from tracefix.capabilities import (
    CallerOrigin,
    CapabilityCatalog,
    CapabilityPolicy,
    ExecutionProfile,
    ToolDispatcher,
)
from tracefix.exceptions import RunConfigurationError
from tracefix.messages import ToolCall
from tracefix.source_snapshot import SourceSnapshotProvider, WorkspaceSourceSnapshotProvider
from tracefix.tools.base import BaseTool, ToolResult, ToolSpec

_METHODS = {
    "mcp_serena_symbols": ("get_symbols_overview", ("relative_path",)),
    "mcp_serena_find_symbol": ("find_symbol", ("name_path_pattern",)),
    "mcp_serena_references": ("find_referencing_symbols", ("name_path", "relative_path")),
}
_PROPERTIES: dict[str, dict[str, dict[str, object]]] = {
    "mcp_serena_symbols": {
        "relative_path": {"type": "string"}, "depth": {"type": "integer"},
    },
    "mcp_serena_find_symbol": {
        "name_path_pattern": {"type": "string"},
        "relative_path": {"type": "string"},
        "include_body": {"type": "boolean"},
        "depth": {"type": "integer"},
    },
    "mcp_serena_references": {
        "name_path": {"type": "string"}, "relative_path": {"type": "string"},
    },
}


class MCPPrerequisiteError(RunConfigurationError, RuntimeError):
    """A missing local prerequisite, detected before model initialization."""

    code = "mcp_prerequisite_unavailable"


class SerenaMCP:
    """Own the Docker-backed MCP transport and its bounded source snapshots."""

    def __init__(
        self,
        source_snapshot_provider: SourceSnapshotProvider | Path,
        run_dir: Path,
        image_id: str,
        *,
        timeout_seconds: float = 90, max_output_chars: int = 16_000,
    ) -> None:
        self.source_snapshot_provider = (
            WorkspaceSourceSnapshotProvider(source_snapshot_provider)
            if isinstance(source_snapshot_provider, Path)
            else source_snapshot_provider
        )
        self.run_dir = run_dir.resolve()
        self.image_id = image_id
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self._active: set[str] = set()
        self._capability_dispatcher: ToolDispatcher | None = None

    def bind_capability_dispatcher(self, dispatcher: ToolDispatcher) -> None:
        """Use the session's frozen capability policy for host source exports."""
        self._capability_dispatcher = dispatcher

    def preflight(self) -> None:
        self.preflight_image(self.image_id)

    @staticmethod
    def preflight_image(image_id: str) -> None:
        """Check every Serena prerequisite before a provider model is constructed."""
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
            raise ValueError("Serena MCP requires an immutable Docker sha256 image ID")
        try:
            from mcp import ClientSession  # noqa: F401
        except ImportError as exc:
            raise MCPPrerequisiteError(
                "install tracefix-agent[mcp] for Serena MCP",
                context={"reason_code": "mcp_sdk_missing"},
            ) from exc
        if shutil.which("docker") is None:
            raise MCPPrerequisiteError(
                "Docker CLI is required for isolated Serena MCP",
                context={"reason_code": "docker_cli_missing"},
            )
        try:
            probe = subprocess.run(
                ["docker", "image", "inspect", image_id, "--format", "{{.Id}}"],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MCPPrerequisiteError(
                "Serena MCP Docker image inspection could not complete",
                context={"reason_code": "docker_image_probe_failed"},
            ) from exc
        if probe.returncode or probe.stdout.strip() != image_id:
            raise MCPPrerequisiteError(
                "Serena MCP Docker image ID is unavailable or changed",
                context={"reason_code": "docker_image_unavailable"},
            )

    def tools(self) -> tuple[BaseTool, ...]:
        return tuple(_SerenaTool(self, name) for name in _METHODS)

    def close(self) -> None:
        for name in tuple(self._active):
            if not self._remove_container(name):
                raise RuntimeError(f"Serena MCP container cleanup failed: {name}")

    def _remove_container(self, name: str) -> bool:
        try:
            result = subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode == 0 or b"No such container" in result.stderr:
            self._active.discard(name)
            return True
        return False

    def _snapshot(self, target: Path) -> tuple[str, str]:
        dispatcher = self._capability_dispatcher
        if dispatcher is None:
            catalog = CapabilityCatalog.for_tool_names(("source_snapshot_export",))
            dispatcher = ToolDispatcher(
                CapabilityPolicy(catalog, ExecutionProfile.WORKSPACE_WRITE)
            )
        decision = dispatcher.authorize(
            "source_snapshot_export", CallerOrigin.HOST_LIFECYCLE
        )
        if not decision.allowed:
            raise RuntimeError(
                "source snapshot export denied by capability policy: "
                + decision.reason_code
            )
        identity = self.source_snapshot_provider.materialize(target)
        return identity.sha256, decision.policy_sha256

    def call(self, method: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        safe_id = hashlib.sha256(call_id.encode()).hexdigest()[:12]
        run_tag = hashlib.sha256(str(self.run_dir).encode()).hexdigest()[:12]
        name = f"tracefix-mcp-{run_tag}-{safe_id}"
        with tempfile.TemporaryDirectory(prefix="mcp-source-", dir=self.run_dir) as temporary:
            snapshot = Path(temporary) / "source"
            snapshot.mkdir()
            source_hash, source_policy_sha256 = self._snapshot(snapshot)
            # Docker's fixed unprivileged UID must be able to traverse the
            # short-lived read-only bind source created with mkdtemp mode 0700.
            snapshot.chmod(0o755)
            self._active.add(name)
            try:
                bounded = {**arguments, "max_answer_chars": self.max_output_chars * 2}
                response = asyncio.run(self._call_async(name, snapshot, method, bounded))
            finally:
                cleaned = self._remove_container(name)
            if not cleaned:
                raise RuntimeError(f"Serena MCP container cleanup failed: {name}")
        if response.isError:
            detail = _content_text(response)[: self.max_output_chars]
            raise RuntimeError("Serena MCP service rejected the query (untrusted data): " + detail)
        output = _content_text(response)
        if len(output) > self.max_output_chars:
            output = output[: self.max_output_chars] + "\n[TraceFix MCP output truncated]"
        return {
            "text": output,
            "source_sha256": source_hash,
            "source_export_policy_sha256": source_policy_sha256,
            "service": "serena/1.7.0",
        }

    async def _call_async(
        self, name: str, snapshot: Path, method: str, arguments: dict[str, Any],
    ) -> Any:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        args = [
            "run", "--rm", "-i", "--name", name, "--network", "none",
            "--read-only", "--user", "10001:10001", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "128",
            "--memory", "1g", "--cpus", "1", "--mount",
            f"type=bind,src={snapshot},dst=/input,readonly",
            "--tmpfs", "/tmp:rw,nosuid,size=512m", "-e", "HOME=/tmp",
            "-e", "SERENA_HOME=/tmp/serena", self.image_id,
            "tracefix-serena-mcp",
        ]
        params = StdioServerParameters(command="docker", args=args, env={
            key: value for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
        })
        async with asyncio.timeout(self.timeout_seconds):
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    if method not in names:
                        raise RuntimeError(f"Serena MCP tool unavailable: {method}")
                    return await session.call_tool(method, arguments)


def _content_text(response: Any) -> str:
    return "\n".join(item.text for item in response.content if item.type == "text")


class _SerenaTool(BaseTool):
    def __init__(self, manager: SerenaMCP, name: str) -> None:
        self.manager = manager
        self._name = name

    @property
    def spec(self) -> ToolSpec:
        method, required = _METHODS[self._name]
        return ToolSpec(
            name=self._name,
            description=(
                f"Read-only Serena symbol query ({method}) over the current task source snapshot."
            ),
            input_schema={
                "type": "object", "properties": _PROPERTIES[self._name],
                "required": list(required), "additionalProperties": False,
            },
        )

    def execute(self, call: ToolCall) -> ToolResult:
        started = time.monotonic()
        try:
            if call.name != self._name:
                raise ValueError("MCP tool name mismatch")
            method, required = _METHODS[self._name]
            if set(call.arguments) - set(_PROPERTIES[self._name]):
                raise ValueError("unknown Serena MCP argument")
            if set(required) - set(call.arguments):
                raise ValueError("missing required Serena MCP argument")
            for key, value in call.arguments.items():
                expected = _PROPERTIES[self._name][key]["type"]
                if (expected == "string" and not isinstance(value, str)) or (
                    expected == "boolean" and not isinstance(value, bool)
                ) or (
                    expected == "integer"
                    and (not isinstance(value, int) or isinstance(value, bool))
                ):
                    raise ValueError(f"invalid Serena MCP argument: {key}")
                if isinstance(value, str) and (len(value) > 500 or "\x00" in value):
                    raise ValueError(f"Serena MCP argument too long: {key}")
                if key == "relative_path" and value:
                    path = Path(value)
                    if path.is_absolute() or ".." in path.parts or "\\" in value:
                        raise ValueError("Serena MCP path must remain within snapshot")
            output = self.manager.call(method, dict(call.arguments), call.id)
            return ToolResult(
                call_id=call.id, tool_name=self._name, success=True, output=output,
                metadata={"backend": "mcp", "service": "serena/1.7.0"},
                duration_ms=(time.monotonic() - started) * 1000,
            )
        except Exception as exc:
            return ToolResult(
                call_id=call.id, tool_name=self._name, success=False,
                error=f"Serena MCP {type(exc).__name__}: {exc}",
                metadata={"backend": "mcp", "service": "serena/1.7.0"},
                duration_ms=(time.monotonic() - started) * 1000,
            )
