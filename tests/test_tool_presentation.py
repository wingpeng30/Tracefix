"""模型可见工具结果精简的单元测试。"""

import json

import pytest

from tracefix import ToolPresentationConfig, ToolResult, ToolResultPresenter


@pytest.mark.parametrize("name", ["get_task_state", "update_task_state"])
def test_task_state_control_fields_survive_large_state_presentation(name) -> None:
    output = {"revision": 7, "evidence_role": "working_state_only"}
    if name == "get_task_state":
        output["state"] = {"requirements": ["retained user text" * 1000]}
    else:
        output["created_ids"] = ["plan-1"]
    result = ToolResult(call_id="state-call", tool_name=name, success=True, output=output)
    payload = json.loads(ToolResultPresenter(
        ToolPresentationConfig(max_generic_chars=256)
    ).present(result))
    assert payload["output"]["revision"] == 7
    assert payload["output"]["evidence_role"] == "working_state_only"
    if name == "get_task_state":
        assert "truncated_output" in payload["output"]["state"]
        assert len(result.output["state"]["requirements"][0]) > 256
    else:
        assert payload["output"]["created_ids"] == ["plan-1"]


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


def test_artifact_search_presentation_keeps_bounded_opaque_locators() -> None:
    result = ToolResult(
        call_id="artifact-search",
        tool_name="search_tool_artifact",
        success=True,
        output={
            "query": "needle",
            "truncated": False,
            "queries_remaining": 63,
            "characters_remaining": 128_000,
            "evidence_role": "historical_tool_output_only",
            "hits": [
                {
                    "artifact": {"artifact_id": f"{index:032x}", "integrity": "verified",
                                 "complete": True},
                    "stream": "stderr",
                    "offset": index * 10,
                    "excerpt": "needle" * 1000,
                }
                for index in range(8)
            ],
        },
    )
    payload = json.loads(ToolResultPresenter().present(result))
    assert len(payload["output"]["hits"]) == 6
    assert payload["output"]["hits"][0]["artifact_id"] == "0" * 32
    assert payload["output"]["hits"][0]["stream"] == "stderr"
    assert len(payload["output"]["hits"][0]["excerpt"]) <= 4000
