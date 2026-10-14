"""The Docker artifact replay always reads the saved log before editing source."""

import json

from examples.replay_artifact_recovery import ArtifactReplayClient
from tracefix import ToolResult, ToolResultPresenter


def _message(name: str, output: dict, *, success: bool = True) -> dict:
    result = ToolResult(
        call_id=f"call-{name}",
        tool_name=name,
        success=success,
        output=output,
        error=None if success else "recorded failure",
    )
    return {
        "role": "tool",
        "content": ToolResultPresenter().present(result),
    }


def _next(messages: list[dict]) -> tuple[str | None, dict | None]:
    response = ArtifactReplayClient().completion(messages=messages, tools=[{}])
    call = response["choices"][0]["message"]["tool_calls"]
    if not call:
        return None, None
    function = call[0]["function"]
    return function["name"], json.loads(function["arguments"])


def test_recovery_replay_uses_search_locator_and_reads_before_patch() -> None:
    failed_test = _message("run_tests", {"test_status": "test_failure"}, success=False)
    search_result = _message(
        "search_tool_artifact",
        {
            "query": "G2_CONTAINER_MIDLOG",
            "hits": [{
                "artifact": {"artifact_id": "a" * 32},
                "stream": "stdout",
                "offset": 50000,
                "excerpt": "G2_CONTAINER_MIDLOG",
            }],
        },
    )
    read_result = _message(
        "read_tool_artifact",
        {"artifact_id": "a" * 32, "content": "G2_CONTAINER_MIDLOG"},
    )
    patch_result = _message("apply_patch", {"changed": True})
    passed_test = _message("run_tests", {"test_status": "passed"})
    diff_result = _message("get_git_diff", {"diff": "patch"})

    assert _next([])[0] == "run_tests"
    assert _next([failed_test])[0] == "search_tool_artifact"
    name, arguments = _next([failed_test, search_result])
    assert name == "read_tool_artifact"
    assert arguments == {
        "artifact_id": "a" * 32,
        "stream": "stdout",
        "offset": 50000,
        "limit": 4096,
    }
    assert _next([failed_test, search_result, read_result])[0] == "apply_patch"
    assert _next([failed_test, search_result, read_result, patch_result])[0] == "run_tests"
    assert _next([
        failed_test, search_result, read_result, patch_result, passed_test
    ])[0] == "get_git_diff"
    assert _next([
        failed_test, search_result, read_result, patch_result, passed_test, diff_result
    ]) == (None, None)
