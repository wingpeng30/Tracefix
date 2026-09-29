"""Replay recorded LiteLLM-shaped responses through a real ordinary repository run.

No provider module or network client is constructed. This is a harness check, not
evidence that a live model can solve the issue.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from tracefix import RunConfig, TraceFixRunner
from tracefix.cli import _ordinary_settings, build_parser
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.report import render_report

PATCH = """diff --git a/widget.py b/widget.py
--- a/widget.py
+++ b/widget.py
@@ -1,2 +1,2 @@
 def next_page(page):
-    return page
+    return page + 1
"""


class ReplayClient:
    def __init__(self) -> None:
        self.calls = 0
        self.sequence = [
            ("run_tests", {"command": "pytest -q tests/test_widget.py"}),
            ("read_file", {"path": "widget.py"}),
            ("apply_patch", {"patch": PATCH}),
            ("run_tests", {"command": "pytest -q tests/test_widget.py"}),
            ("get_git_diff", {"context_lines": 3}),
        ]

    def token_counter(self, **_kwargs):
        return 100

    def completion(self, **kwargs):
        self.calls += 1
        if not kwargs.get("tools") or not kwargs.get("messages"):
            raise AssertionError("real tool schema and messages were not sent to adapter")
        call = self.sequence[self.calls - 1] if self.calls <= len(self.sequence) else None
        tool_calls = (
            []
            if call is None
            else [
                {
                    "id": f"replay-{self.calls}",
                    "type": "function",
                    "function": {"name": call[0], "arguments": json.dumps(call[1])},
                }
            ]
        )
        return {
            "model": "offline/replay",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
            },
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "离线回放结束" if call is None else None,
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": "stop" if call is None else "tool_calls",
                }
            ],
        }


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if output.exists():
        parser.error("output must not already exist")
    repo = output / "source"
    repo.mkdir(parents=True)
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (repo / "widget.py").write_text("def next_page(page):\n    return page\n", encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_widget.py").write_text(
        "from widget import next_page\n\ndef test_next_page():\n    assert next_page(1) == 2\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "--all")
    _git(
        repo,
        "-c",
        "user.name=Replay",
        "-c",
        "user.email=replay@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    config_path = output / "config.toml"
    config_path.write_text(
        "[run]\n"
        'repo = "source"\n'
        'task = "修复分页测试"\n'
        'test_target = "tests/test_widget.py"\n'
        'source_import = "widget"\n'
        'model = "offline/replay"\n'
        'output_dir = "runs"\n',
        encoding="utf-8",
    )
    settings = _ordinary_settings(
        build_parser().parse_args(
            [
                "doctor",
                "--config",
                str(config_path),
                "--repo",
                str(repo),
                "--model",
                "offline/replay",
                "--output-dir",
                str(output / "runs"),
                "--test-target",
                "tests/test_widget.py",
                "--source-import",
                "widget",
            ]
        )
    )
    client = ReplayClient()
    result = TraceFixRunner(llm_factory=lambda config: LiteLLMAdapter(config, client=client)).run(
        RunConfig(
            repo=settings["repo"],
            task=settings["task"],
            model_name=settings["model_name"],
            output_dir=settings["output_dir"],
            env_file=None,
            test_python_executable=Path(sys.executable),
            test_target=settings["test_target"],
            source_import=settings["source_import"],
        )
    )
    report = render_report(Path(result.result_path).parent)
    print(
        json.dumps(
            {
                "status": result.status.value,
                "model_requests": client.calls,
                "result": result.result_path,
                "report": str(report),
            },
            ensure_ascii=False,
        )
    )
    return (
        0
        if result.status.value == "completed" and result.agent_validation_status == "verified"
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
