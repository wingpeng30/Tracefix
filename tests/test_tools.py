from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from tracefix import (
    RESERVED_TOOL_NAMES,
    BaseTool,
    ToolAlreadyRegisteredError,
    ToolCall,
    ToolNotFoundError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


@dataclass
class StubTool(BaseTool):
    _spec: ToolSpec

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(call_id=call.id, tool_name=self.spec.name, success=True, output="ok")


def make_tool(name: str = "search_code") -> StubTool:
    return StubTool(
        ToolSpec(
            name=name,
            description="Search source files",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )
    )


def test_reserved_tool_names_are_stable() -> None:
    assert RESERVED_TOOL_NAMES == {
        "search_code",
        "read_file",
        "apply_patch",
        "run_tests",
        "get_git_diff",
    }


def test_registry_registers_and_returns_detached_specs() -> None:
    tool = make_tool()
    registry = ToolRegistry([tool])
    specs = registry.specs()
    specs[0].input_schema["properties"] = {}

    assert registry.get("search_code") is tool
    assert registry.names == ("search_code",)
    assert registry.specs()[0].input_schema["properties"] != {}


def test_registry_rejects_duplicates_and_unknown_names() -> None:
    registry = ToolRegistry([make_tool()])
    with pytest.raises(ToolAlreadyRegisteredError):
        registry.register(make_tool())
    with pytest.raises(ToolNotFoundError) as captured:
        registry.get("missing")
    assert captured.value.context["available_tools"] == ["search_code"]


def test_tool_models_validate_schema_and_result_state() -> None:
    with pytest.raises(ValidationError):
        ToolSpec(name="bad", description="bad", input_schema={"type": "array"})
    with pytest.raises(ValidationError):
        ToolResult(call_id="1", tool_name="bad", success=False)

    result = ToolResult(call_id="1", tool_name="ok", success=True, output={"matches": 2})
    assert result.model_dump(mode="json")["output"] == {"matches": 2}


