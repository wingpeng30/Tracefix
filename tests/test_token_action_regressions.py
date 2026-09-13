"""真实预筛选暴露的 Token 行动优化回归用例。"""

import json
import warnings

from tracefix import (
    AgentConfig,
    BaseLLM,
    BaseTool,
    LLMConfig,
    LLMResponse,
    Message,
    MessageRole,
    MinimalAgent,
    TokenUsage,
    ToolCall,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from tracefix.repository import RepositoryIndexer


class _ScriptedLLM(BaseLLM):
    """按顺序返回预设响应，避免回归测试访问真实供应商。"""

    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(LLMConfig(model_name="scripted"))
        self._responses = iter(responses)

    def complete(self, messages, tools=()):
        return next(self._responses)


class _NoEffectPatchTool(BaseTool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="apply_patch", description="测试空补丁反馈")

    def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.id,
            tool_name=call.name,
            success=False,
            error="patch does not change the target file",
        )


def _response(*calls: ToolCall, content: str | None = None) -> LLMResponse:
    return LLMResponse(
        message=Message(role=MessageRole.ASSISTANT, content=content, tool_calls=calls),
        usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2, cost_usd=0),
        model_name="scripted",
        finish_reason="tool_calls" if calls else "stop",
    )


def test_no_effect_patch_adds_immediate_recovery_guidance() -> None:
    """空补丁应在下一次模型请求前得到明确恢复指引。"""
    agent = MinimalAgent(
        _ScriptedLLM(
            [
                _response(ToolCall(id="patch", name="apply_patch", arguments={"patch": "x"})),
                _response(content="结束"),
            ]
        ),
        ToolRegistry([_NoEffectPatchTool()]),
        AgentConfig(),
    )
    agent.run("修复问题")
    assert any(
        message.metadata.get("kind") == "no_effect_patch"
        for message in agent.history.snapshot()
    )


def test_indexer_suppresses_fixture_syntaxwarning(tmp_path) -> None:
    """故意非法的上游诊断夹具不应污染索引阶段的控制台输出。"""
    (tmp_path / "fixture.py").write_text("value = '\\z'\n", encoding="utf-8")
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", SyntaxWarning)
        index = RepositoryIndexer(tmp_path).build()
    assert index.files[0].path == "fixture.py"
    assert not [item for item in captured if issubclass(item.category, SyntaxWarning)]


def test_search_projection_tells_model_to_refine_omitted_matches() -> None:
    """搜索裁剪必须显式披露遗漏命中，避免模型误以为结果全集只有六条。"""
    from tracefix import ToolResultPresenter

    result = ToolResult(
        call_id="search",
        tool_name="search_code",
        success=True,
        output={"matches": [{"path": f"pkg/{index}.py"} for index in range(8)]},
    )
    payload = json.loads(ToolResultPresenter().present(result))
    assert payload["output"]["matches_omitted"] == 2
    assert "path" in payload["output"]["next_action"]
