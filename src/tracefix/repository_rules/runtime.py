"""Small runtime wiring over explicit source, selection and metadata ports."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tracefix.messages import ToolCall
from tracefix.repository_rules.access import ToolAccessPlanner
from tracefix.repository_rules.catalog import RepositoryRules
from tracefix.repository_rules.delivery import RuleDeliveryLedger
from tracefix.repository_rules.file_io import BoundedWorkspaceReader
from tracefix.repository_rules.gate import AccessPathResolver, RuleAccessGate, RuleSelector
from tracefix.repository_rules.git_metadata import GitMetadataPaths
from tracefix.repository_rules.mcp_output import McpSourceGate
from tracefix.repository_rules.mcp_snapshot import SnapshotMcpRuleGuard
from tracefix.repository_rules.metadata import WorkspaceAccessPaths
from tracefix.repository_rules.search import MAX_SEARCH_SOURCE_BYTES, PagedRepositorySearch
from tracefix.repository_rules.source import RuleSourceView
from tracefix.source_view import SourceView
from tracefix.tools.base import ToolRegistry, ToolSpec
from tracefix.tools.repository_search import RepositorySearchTool

_NON_SOURCE_TOOLS = frozenset({
    "load_skill", "read_tool_artifact", "search_tool_artifact", "get_task_state",
    "update_task_state", "get_job_status", "read_job_output", "cancel_job", "compact_context",
})
_SERENA_TOOLS = frozenset({"mcp_serena_symbols", "mcp_serena_find_symbol", "mcp_serena_references"})


class SourceGuardBinding(Protocol):
    def bind_source_result_guard(self, guard: SnapshotMcpRuleGuard) -> None: ...


@dataclass(frozen=True)
class RepositoryRuleRuntime:
    tools: ToolRegistry
    gate: RuleAccessGate


@dataclass(frozen=True)
class WorkspaceRulePorts:
    selector: RuleSelector
    metadata: AccessPathResolver
    source: SourceView
    case_sensitive: bool


class WorkspaceSource:
    def __init__(self, reader: BoundedWorkspaceReader) -> None:
        self.reader = reader

    def read_bytes(self, target: str) -> bytes | None:
        return self.reader.read_bytes(target, maximum=MAX_SEARCH_SOURCE_BYTES)


def assemble_rules(tools: ToolRegistry, *, run_id: str, selector: RuleSelector,
                   metadata: AccessPathResolver, source: SourceView, case_sensitive: bool,
                   pytest_config: str | None = None,
                   executor_specs: tuple[ToolSpec, ...] | None = None,
                   mcp: SourceGuardBinding | None = None) -> RepositoryRuleRuntime:
    """Replace only the optional search schema; preserve every other tool binding."""
    planner, delivery = ToolAccessPlanner(pytest_config=pytest_config), RuleDeliveryLedger()

    def candidates(args):
        # Planning is pure; the injected resolver enumerates metadata only.
        call = ToolCall(id="rules-search-metadata", name="search_code",
                        arguments=args.model_dump(exclude={"cursor"}))
        return metadata.resolve(planner.plan(call))

    search = RepositorySearchTool(PagedRepositorySearch(
        run_id, candidates=candidates, source=source, selector=selector, delivery=delivery,
        case_sensitive_paths=case_sensitive))
    registry = ToolRegistry((search if tool.spec.name == "search_code" else tool for tool in tools),
                            executor_specs=executor_specs)
    validators = {}
    if mcp is not None:
        guard = SnapshotMcpRuleGuard(McpSourceGate(selector, delivery))
        mcp.bind_source_result_guard(guard)
        validators = {name: guard for name in _SERENA_TOOLS}
    gate = RuleAccessGate(planner, selector, delivery, resolver=metadata,
                          non_source_tools=_NON_SOURCE_TOOLS,
                          search_validator=search, source_validators=validators, run_id=run_id)
    return RepositoryRuleRuntime(registry, gate)


def assemble_local_rules(tools: ToolRegistry, *, root: Path, run_id: str,
                         protected_dirs: tuple[str, ...] = (), pytest_config: str | None = None,
                         mcp: SourceGuardBinding | None = None) -> RepositoryRuleRuntime:
    ports = workspace_rule_ports(root, protected_dirs=protected_dirs)
    return assemble_rules(tools, run_id=run_id, selector=ports.selector, metadata=ports.metadata,
                          source=ports.source, case_sensitive=ports.case_sensitive,
                          pytest_config=pytest_config, mcp=mcp)


def workspace_rule_ports(root: Path, *,
                          protected_dirs: tuple[str, ...] = ()) -> WorkspaceRulePorts:
    reader = BoundedWorkspaceReader(root, protected_dirs=protected_dirs)
    case_sensitive = os.name != "nt"
    selector = RepositoryRules(RuleSourceView(root, protected_dirs=protected_dirs),
                               case_sensitive=case_sensitive,
                               validate_target=reader.validate_target)
    metadata = WorkspaceAccessPaths(root, validate_target=reader.validate_target,
                                    index_paths=GitMetadataPaths(root),
                                    case_sensitive=case_sensitive)
    return WorkspaceRulePorts(selector, metadata, WorkspaceSource(reader), case_sensitive)
