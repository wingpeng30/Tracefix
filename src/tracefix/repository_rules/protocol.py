"""Versioned, host-only rule ports; these are never model ToolSpecs."""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from tracefix.repository_rules.access import PathAccess, ToolAccessPlan
from tracefix.repository_rules.domain import RuleSelection
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.gate import AccessPathResolver, RuleSelector
from tracefix.repository_rules.paths import relative_path, validate_glob
from tracefix.source_view import SourceView
from tracefix.tools.base import ToolResult

RULE_PROTOCOL_VERSION = 1
RULE_CONTROL_NAME = "__repository_rules__"
RULE_CONTROL_CAPABILITY = "repository_rules_read"


class _Path(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    path: str = Field(max_length=1024)
    purpose: Literal["read", "write", "test", "config", "search_root"]


class _Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    tool_name: Literal["read_file", "apply_patch", "run_tests", "start_test_job",
                       "search_code", "get_git_diff"]
    paths: list[_Path] = Field(max_length=128)
    enumeration: Literal["none", "search", "test_targets", "changed_files"]
    search_glob: str | None = Field(default=None, max_length=512)

    def access_plan(self) -> ToolAccessPlan:
        for path in self.paths:
            relative_path(path.path, allow_root=path.purpose in {"test", "search_root"})
        if self.search_glob is not None:
            validate_glob(self.search_glob)
        if self.enumeration == "search" and (
            len(self.paths) != 1 or self.paths[0].purpose != "search_root"
            or self.search_glob is None
        ):
            raise RuleError("rules_protocol_invalid_plan")
        paths = tuple(PathAccess(p.path, p.purpose) for p in self.paths)
        return ToolAccessPlan(self.tool_name, paths,
                              self.enumeration, self.search_glob)


class RulePortRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: StrictInt = Field(ge=1, le=1)
    operation: Literal["select", "paths", "read"]
    target: str | None = Field(default=None, max_length=1024)
    plan: _Plan | None = None


def execute_rule_port(arguments: dict, *, selector: RuleSelector,
                      metadata: AccessPathResolver, source: SourceView) -> dict:
    """Caller must authorize the private control before invoking this adapter."""
    try:
        request = RulePortRequest.model_validate(arguments)
    except ValidationError as exc:
        raise RuleError("rules_protocol_invalid_request") from exc
    if request.operation == "paths":
        if request.plan is None or request.target is not None:
            raise RuleError("rules_protocol_invalid_request")
        paths = tuple(metadata.resolve(request.plan.access_plan()))
        if len(paths) > 5000:
            raise RuleError("rules_access_path_limit")
        payload = {"paths": [relative_path(path) for path in paths]}
    else:
        if request.target is None or request.plan is not None:
            raise RuleError("rules_protocol_invalid_request")
        target = relative_path(request.target)
        if request.operation == "select":
            selection = selector.select(target)
            payload = {"selection": selection.model_dump(mode="json")}
        else:
            data = source.read_bytes(target)
            if data is not None and (not isinstance(data, bytes) or len(data) > 1_000_000):
                raise RuleError("rules_source_limit_exceeded")
            payload = {"content_base64": (base64.b64encode(data).decode("ascii")
                                           if data is not None else None)}
    return {"version": RULE_PROTOCOL_VERSION, "operation": request.operation, **payload}


class RuleChannel(Protocol):
    def call(self, name: str, arguments: dict) -> ToolResult: ...


class RemoteRulePorts:
    """Source bytes stay bounded; opaque search state and delivery remain host-owned."""

    def __init__(self, channel: RuleChannel, authorize: Callable[[], None]) -> None:
        self.channel, self.authorize = channel, authorize

    def _request(self, operation: str, **arguments) -> dict:
        self.authorize()
        result = self.channel.call(RULE_CONTROL_NAME, {
            "version": RULE_PROTOCOL_VERSION, "operation": operation, **arguments})
        output = result.output
        if (not result.success or not isinstance(output, dict)
                or type(output.get("version")) is not int
                or output.get("version") != RULE_PROTOCOL_VERSION
                or output.get("operation") != operation):
            raise RuleError("rules_protocol_response_invalid")
        return output

    def select(self, target: str) -> RuleSelection:
        output = self._request("select", target=relative_path(target))
        try:
            selection = RuleSelection.model_validate(output.get("selection"))
            if selection.target_path != target:
                raise RuleError("rules_protocol_target_mismatch")
            return selection
        except ValidationError as exc:
            raise RuleError("rules_protocol_selection_invalid") from exc

    def resolve(self, plan: ToolAccessPlan) -> tuple[str, ...]:
        value = {"tool_name": plan.tool_name,
                 "paths": [{"path": path.path, "purpose": path.purpose} for path in plan.paths],
                 "enumeration": plan.enumeration, "search_glob": plan.search_glob}
        try:
            _Plan.model_validate(value).access_plan()
        except ValidationError as exc:
            raise RuleError("rules_protocol_invalid_plan") from exc
        output = self._request("paths", plan=value)
        paths = output.get("paths")
        if not isinstance(paths, list) or len(paths) > 5000:
            raise RuleError("rules_protocol_paths_invalid")
        return tuple(relative_path(path) for path in paths)

    def read_bytes(self, target: str) -> bytes | None:
        output = self._request("read", target=relative_path(target))
        value = output.get("content_base64")
        if "content_base64" not in output:
            raise RuleError("rules_protocol_source_invalid")
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 1_333_336:
            raise RuleError("rules_protocol_source_invalid")
        try:
            data = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise RuleError("rules_protocol_source_invalid") from exc
        if len(data) > 1_000_000:
            raise RuleError("rules_source_limit_exceeded")
        return data
