"""Pure pinned Serena tool contracts, shared by adapter and rule validation."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from tracefix.messages import ToolCall

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



class SourceOutputGuard(Protocol):
    def filter(self, method: str, arguments: dict, text: str, snapshot: Path,
               source_sha256: str) -> dict: ...


def validate_serena_call(call: ToolCall, expected_name: str | None = None) -> tuple[str, dict]:
    if expected_name is not None and call.name != expected_name:
        raise ValueError("MCP tool name mismatch")
    if call.name not in _METHODS:
        raise ValueError("unknown Serena MCP tool")
    method, required = _METHODS[call.name]
    if set(call.arguments) - set(_PROPERTIES[call.name]):
        raise ValueError("unknown Serena MCP argument")
    if set(required) - set(call.arguments):
        raise ValueError("missing required Serena MCP argument")
    for key, value in call.arguments.items():
        expected = _PROPERTIES[call.name][key]["type"]
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
    return method, dict(call.arguments)
