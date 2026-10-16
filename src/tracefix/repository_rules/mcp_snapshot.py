"""Runtime adapter from an approved MCP snapshot to source-claim verification."""

from __future__ import annotations

from pathlib import Path

from tracefix.messages import ToolCall
from tracefix.repository_rules.access import ToolAccessPlanner
from tracefix.repository_rules.file_io import BoundedWorkspaceReader
from tracefix.repository_rules.mcp_output import McpSourceGate
from tracefix.repository_rules.metadata import WorkspaceAccessPaths


class _SnapshotSource:
    def __init__(self, root: Path) -> None:
        self.reader = BoundedWorkspaceReader(root)

    def read_bytes(self, target: str) -> bytes | None:
        return self.reader.read_bytes(target, maximum=1_000_000)


class SnapshotMcpRuleGuard:
    def __init__(self, source_gate: McpSourceGate) -> None:
        self.source_gate = source_gate

    def validate_call(self, call: ToolCall) -> None:
        self.source_gate.validate_call(call)

    def filter(self, method: str, arguments: dict, text: str, snapshot: Path,
               source_sha256: str) -> dict:
        # The approved snapshot can contain build/dist source; include it here.
        # Provenance enumeration never follows aliases or opens source bodies.
        metadata = WorkspaceAccessPaths(snapshot, ignored_dirs=frozenset())
        plan = ToolAccessPlanner().plan(ToolCall(id="mcp-source-metadata", name="search_code",
                                                arguments={"query": "source provenance"}))
        paths = metadata.resolve(plan)
        return self.source_gate.filter(method, arguments, text, source_files=paths,
                                       source=_SnapshotSource(snapshot),
                                       source_sha256=source_sha256)
