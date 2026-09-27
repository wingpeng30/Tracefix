"""工具协议、注册表与内置实现。"""

from tracefix.tools.base import (
    RESERVED_TOOL_NAMES,
    BaseTool,
    ReservedToolName,
    SkillCatalogEntry,
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
from tracefix.tools.skills import SkillActivationTool, SkillLimits

__all__ = [
    "RESERVED_TOOL_NAMES",
    "ApplyPatchTool",
    "BaseTool",
    "GetGitDiffTool",
    "ReadFileTool",
    "ReservedToolName",
    "RunTestsTool",
    "SearchCodeTool",
    "SkillCatalogEntry",
    "SkillActivationTool",
    "SkillLimits",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "create_default_tool_registry",
]
