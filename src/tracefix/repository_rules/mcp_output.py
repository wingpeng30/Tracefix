"""Verify pinned Serena source claims before delivering any source response."""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from itertools import groupby

from pydantic import ValidationError

from tracefix.mcp_contracts import validate_serena_call
from tracefix.messages import ToolCall
from tracefix.repository_rules.domain import RuleSelection
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.gate import RuleDelivery, RuleSelector
from tracefix.repository_rules.paths import relative_path
from tracefix.source_view import SourceView

MAX_MCP_PATHS = 128
MAX_MCP_TEXT_CHARS = 16000
_FIELDS = frozenset({"name", "name_path", "kind", "relative_path", "body_location",
                     "body", "children", "content_around_reference"})


@dataclass(frozen=True)
class SourceSnippet:
    path: str
    text: str
    kind: str
    start_line: int | None = None
    end_line: int | None = None


@dataclass(frozen=True)
class McpSourceClaims:
    paths: tuple[str, ...]
    snippets: tuple[SourceSnippet, ...]


def extract_serena_claims(method: str, arguments: dict, text: str,
                         source_files: Collection[str]) -> McpSourceClaims:
    if method not in {"get_symbols_overview", "find_symbol", "find_referencing_symbols"}:
        raise RuleError("rules_mcp_method_unknown")
    if not isinstance(text, str) or len(text) > MAX_MCP_TEXT_CHARS:
        raise RuleError("rules_mcp_output_limit")

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise RuleError("rules_mcp_output_ambiguous")
            value[key] = item
        return value

    def invalid_constant(value):
        raise RuleError("rules_mcp_output_unverifiable")

    try:
        payload = json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except (ValueError, RecursionError) as exc:
        raise RuleError("rules_mcp_output_unverifiable") from exc
    if not isinstance(payload, (dict, list)):
        raise RuleError("rules_mcp_output_unverifiable")
    known = frozenset(source_files)
    paths, snippets, nodes = set(), [], 0

    def path(value):
        value = relative_path(value)
        if value not in known:
            raise RuleError("rules_mcp_source_not_in_snapshot")
        paths.add(value)
        if len(paths) > MAX_MCP_PATHS:
            raise RuleError("rules_mcp_source_path_limit")
        return value

    explicit = arguments.get("relative_path")
    if explicit is not None and not isinstance(explicit, str):
        raise RuleError("rules_mcp_arguments_invalid")
    inherited = (path(explicit) if method == "get_symbols_overview" else
                 path(explicit) if method == "find_symbol" and explicit in known else None)

    def walk(value, origin, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > 2000 or depth > 32:
            raise RuleError("rules_mcp_output_structure_limit")
        if isinstance(value, list):
            for child in value:
                walk(child, origin, depth + 1)
            return
        if isinstance(value, dict):
            if not value:
                return
            if set(value) & (_FIELDS - {"kind"}):
                if set(value) - _FIELDS:
                    raise RuleError("rules_mcp_output_unverifiable")
                current = path(value["relative_path"]) if "relative_path" in value else origin
                if current is None:
                    raise RuleError("rules_mcp_source_path_missing")
                paths.add(current)
                if any(key in value and not isinstance(value[key], str)
                       for key in ("name", "name_path", "kind")):
                    raise RuleError("rules_mcp_output_unverifiable")
                if "body_location" in value:
                    location = value["body_location"]
                    if (not isinstance(location, dict)
                            or set(location) != {"start_line", "end_line"}
                            or any(item is not None and type(item) is not int
                                   for item in location.values())):
                        raise RuleError("rules_mcp_body_location_invalid")
                if value.get("body") is not None:
                    location = value.get("body_location")
                    if (not isinstance(value["body"], str) or not isinstance(location, dict)
                            or type(location.get("start_line")) is not int
                            or type(location.get("end_line")) is not int
                            or not 0 <= location["start_line"] <= location["end_line"]):
                        raise RuleError("rules_mcp_body_location_invalid")
                    snippets.append(SourceSnippet(current, value["body"], "body",
                                                  location["start_line"], location["end_line"]))
                if "content_around_reference" in value:
                    if not isinstance(value["content_around_reference"], str):
                        raise RuleError("rules_mcp_output_unverifiable")
                    snippets.append(SourceSnippet(current, value["content_around_reference"],
                                                  "reference"))
                if "children" in value:
                    walk(value["children"], current, depth + 1)
                return
            # Pinned Serena can group by kind, filename and child name.
            for key, child in value.items():
                walk(child, path(key) if key in known else origin, depth + 1)
            return
        if origin is None:
            raise RuleError("rules_mcp_source_path_missing")
        if not isinstance(value, (str, int, float, bool)) or value is None:
            raise RuleError("rules_mcp_output_unverifiable")
        paths.add(origin)

    walk(payload, inherited)
    return McpSourceClaims(tuple(sorted(paths)), tuple(snippets))


class McpSourceGate:
    def __init__(self, selector: RuleSelector, delivery: RuleDelivery) -> None:
        self.selector, self.delivery = selector, delivery

    def validate_call(self, call: ToolCall) -> None:
        try:
            method, args = validate_serena_call(call)
        except ValueError as exc:
            raise RuleError("rules_mcp_arguments_invalid") from exc
        target = args.get("relative_path")
        if method == "get_symbols_overview" and not target:
            raise RuleError("rules_mcp_source_path_required")
        if target:
            relative_path(target, allow_root=method == "find_symbol")

    def filter(self, method: str, arguments: dict, text: str, *, source_files: Collection[str],
               source: SourceView, source_sha256: str) -> dict:
        if (not isinstance(source_sha256, str) or len(source_sha256) != 64
                or any(char not in "0123456789abcdef" for char in source_sha256)):
            raise RuleError("rules_mcp_source_identity_invalid")
        claims = extract_serena_claims(method, arguments, text, source_files)
        selections = []
        for target in claims.paths:
            try:
                selected = RuleSelection.model_validate(self.selector.select(target))
            except ValidationError as exc:
                raise RuleError("rules_selection_invalid") from exc
            if selected.target_path != target:
                raise RuleError("rules_selection_target_mismatch")
            selections.append(selected)
        pending = [selected for selected in selections if not self.delivery.is_delivered(selected)]
        refs = [{"path": target, "snapshot_sha256": source_sha256} for target in claims.paths]
        if pending:
            for selected in pending:
                self.delivery.defer(selected)
            return {"text": "", "rules_pending": True,
                    "pending_paths": [selected.target_path for selected in pending],
                    "pending_reason": "rules_pending", "source_refs": refs}
        for target, snippets in groupby(sorted(claims.snippets, key=lambda item: item.path),
                                       key=lambda item: item.path):
            raw = source.read_bytes(target)
            if not isinstance(raw, bytes) or len(raw) > 1_000_000:
                raise RuleError("rules_mcp_source_content_unverifiable")
            try:
                content = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            except UnicodeError as exc:
                raise RuleError("rules_mcp_source_encoding_unsupported") from exc
            lines, kept_lines = content.splitlines(), content.splitlines(keepends=True)
            reference_lines = content.split("\n")
            for snippet in snippets:
                if snippet.kind == "body":
                    if (snippet.end_line > len(lines) or not snippet.text
                            or snippet.text not in "".join(kept_lines[
                                snippet.start_line:snippet.end_line + 1])):
                        raise RuleError("rules_mcp_body_source_mismatch")
                else:
                    for line in snippet.text.splitlines():
                        prefix, separator, excerpt = line.partition(":")
                        number = prefix.strip(" .><+-")
                        if (not separator or not number.isdecimal() or len(number) > 7
                                or int(number) >= len(reference_lines)
                                or excerpt != reference_lines[int(number)]):
                            raise RuleError("rules_mcp_reference_source_mismatch")
        return {"text": text, "rules_pending": False, "source_refs": refs,
                "evidence_role": "untrusted_source_tool_result"}
