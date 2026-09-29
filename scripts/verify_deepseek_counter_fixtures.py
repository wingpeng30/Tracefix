"""Offline request-shape checks for the pinned official DeepSeek renderer."""

from __future__ import annotations

import json

from tracefix.models.input_bounds import count_deepseek_v41_request


def main() -> None:
    base = {
        "model": "deepseek-flash",
        "thinking": {"type": "disabled"},
        "temperature": 0,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": "修复问题。保留 Unicode 😀 和转义 \\n。"},
            {"role": "user", "content": 'read "src/file.py"'},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"src/file.py"}'},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": "résumé = '你好'"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "read_file", "description": "Read a file",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
            }, "required": ["path"]},
        }}],
    }
    complex_result = count_deepseek_v41_request(base)
    assert complex_result.tokens and complex_result.tokens > 0
    assert complex_result.status == "estimate"
    unsupported = count_deepseek_v41_request({**base, "model": "unknown"})
    special = count_deepseek_v41_request({**base, "messages": [
        {"role": "user", "content": "<think>literal"},
    ]})
    nontext = count_deepseek_v41_request({**base, "messages": [
        {"role": "user", "content": [{"type": "image_url", "image_url": "x"}]},
    ]})
    assert {unsupported.status, special.status, nontext.status} == {"unavailable"}
    print(json.dumps({
        "complex_text_tool_request": complex_result.__dict__,
        "unknown_model": unsupported.__dict__,
        "special_marker": special.__dict__,
        "nontext": nontext.__dict__,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
