"""工具协议、注册表与内置实现。"""

from tracefix.tools.base import (
    RESERVED_TOOL_NAMES,
    BaseTool,
    ReservedToolName,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from tracefix.tools.builtin import (
    ApplyPatchTool,
    GetGitDiffTool,
    ReadFileTool,
    RunTestsTool,
    SearchCodeTool,
    create_default_tool_registry,
)

__all__ = [
    "RESERVED_TOOL_NAMES",
    "ApplyPatchTool",
    "BaseTool",
    "GetGitDiffTool",
    "ReadFileTool",
    "ReservedToolName",
    "RunTestsTool",
    "SearchCodeTool",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "create_default_tool_registry",
]
