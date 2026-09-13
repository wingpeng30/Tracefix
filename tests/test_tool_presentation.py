"""模型可见工具结果精简的单元测试。"""

import json

from tracefix import ToolPresentationConfig, ToolResult, ToolResultPresenter


def test_test_output_is_shortened_but_keeps_tail() -> None:
    """pytest 末尾的失败摘要必须保留，完整结果则由轨迹事件审计。"""
    presenter = ToolResultPresenter(ToolPresentationConfig(max_test_output_chars=300))
    result = ToolResult(
        call_id="call-1",
        tool_name="run_tests",
        success=False,
        error="tests failed",
        output={"stdout": "head" * 150, "stderr": "important final traceback"},
    )
    payload = json.loads(presenter.present(result))
    assert "TraceFix 已裁剪" in payload["output"]["stdout"]
    assert payload["output"]["stderr"] == "important final traceback"
    assert "timestamp" not in payload


def test_search_match_count_is_bounded_and_raw_mode_is_lossless() -> None:
    """搜索仅减少模型可见匹配数；关闭优化时 JSON 与 ToolResult 完全一致。"""
    result = ToolResult(
        call_id="call-2",
        tool_name="search_code",
        success=True,
        output={"matches": [{"path": f"a{i}.py"} for i in range(8)]},
        metadata={"duplicate": True},
    )
    compact = json.loads(ToolResultPresenter().present(result))
    assert len(compact["output"]["matches"]) == 6
    assert compact["output"]["matches_omitted"] == 2
    assert compact["metadata"]["duplicate"] is True
    raw = ToolResultPresenter(ToolPresentationConfig(enabled=False)).present(result)
    assert raw == result.model_dump_json()
