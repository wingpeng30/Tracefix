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
from tracefix.onboarding import verify_patch
from tracefix.report import render_report

PATCH = """diff --git a/widget.py b/widget.py
--- a/widget.py
+++ b/widget.py
@@ -1,2 +1,2 @@
 def next_page(page):
-    return page
+    return page + 1
"""

GATE_FIRST_PATCH = """diff --git a/widget.py b/widget.py
--- a/widget.py
+++ b/widget.py
@@ -1,2 +1,2 @@
 def next_page(page):
-    return page
+    return page + 1
diff --git a/identity.py b/identity.py
--- a/identity.py
+++ b/identity.py
@@ -1 +1 @@
-def same(value): return value
+def same(value): return value + 1
"""

GATE_REPAIR_PATCH = """diff --git a/widget.py b/widget.py
--- a/widget.py
+++ b/widget.py
@@ -1,2 +1,4 @@
 def next_page(page):
-    return page + 1
+    if page < 0:
+        return page
+    return page + 1
diff --git a/identity.py b/identity.py
--- a/identity.py
+++ b/identity.py
@@ -1 +1 @@
-def same(value): return value + 1
+def same(value): return value
"""


class ReplayClient:
    def __init__(self, *, mcp_enabled: bool = False, validation_gate: bool = False) -> None:
        self.calls = 0
        self.mcp_enabled = mcp_enabled
        self.validation_gate = validation_gate
        self.sequence = (
            [
                ("mcp_serena_symbols", {"relative_path": "widget.py"}),
                ("mcp_serena_find_symbol", {
                    "name_path_pattern": "next_page", "relative_path": "widget.py",
                }),
            ] if mcp_enabled else []
        ) + ([
            ("apply_patch", {"patch": GATE_FIRST_PATCH}),
            None,
            ("apply_patch", {"patch": GATE_REPAIR_PATCH}),
            None,
        ] if validation_gate else [
            ("run_tests", {"command": "pytest -q tests/test_widget.py"}),
            ("read_file", {"path": "widget.py"}),
            ("apply_patch", {"patch": PATCH}),
            ("run_tests", {"command": "pytest -q tests/test_widget.py"}),
            ("get_git_diff", {"context_lines": 3}),
        ])

    def token_counter(self, **_kwargs):
        return 100

    def completion(self, **kwargs):
        self.calls += 1
        if not kwargs.get("tools") or not kwargs.get("messages"):
            raise AssertionError("real tool schema and messages were not sent to adapter")
        if self.mcp_enabled and self.calls >= 2:
            if not any("next_page" in str(message) for message in kwargs["messages"]):
                raise AssertionError("Serena result did not enter the model request")
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
    parser.add_argument("--mcp-serena-image-id")
    parser.add_argument("--docker-ordinary-image-id")
    parser.add_argument("--regression-example", action="store_true")
    parser.add_argument("--validation-gate-example", action="store_true")
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
    if args.regression_example or args.validation_gate_example:
        (repo / "identity.py").write_text("def same(value):\n    return value\n", encoding="utf-8")
        (tests / "test_backward.py").write_text(
            "from widget import next_page\n\n"
            "def test_negative_sentinel_is_unchanged():\n"
            "    assert next_page(-1) == -1\n",
            encoding="utf-8",
        )
    if args.regression_example:
        (tests / "test_preserved.py").write_text(
            "from identity import same\n\n"
            "def test_identity():\n    assert same(7) == 7\n", encoding="utf-8",
        )
        (tests / "test_still_failed.py").write_text(
            "from widget import next_page\n\n"
            "def test_unrelated_expectation():\n    assert next_page(2) == 99\n",
            encoding="utf-8",
        )
    if args.validation_gate_example:
        (repo / "identity.py").write_text(
            "def same(value): return value\n", encoding="utf-8",
        )
        (tests / "test_identity.py").write_text(
            "from identity import same\n\n"
            "def test_identity():\n    assert same(7) == 7\n", encoding="utf-8",
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
        'output_dir = "runs"\n'
        + ('regression_targets = ["tests/test_backward.py", "tests/test_identity.py"]\n'
           if args.validation_gate_example else ""),
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
    client = ReplayClient(
        mcp_enabled=args.mcp_serena_image_id is not None,
        validation_gate=args.validation_gate_example,
    )
    result = TraceFixRunner(llm_factory=lambda config: LiteLLMAdapter(config, client=client)).run(
        RunConfig(
            repo=settings["repo"],
            task=settings["task"],
            model_name=settings["model_name"],
            output_dir=settings["output_dir"],
            env_file=None,
            test_python_executable=(
                None if args.docker_ordinary_image_id else Path(sys.executable)
            ),
            test_target=settings["test_target"],
            regression_targets=settings["regression_targets"],
            source_import=settings["source_import"],
            mcp_serena_image_id=args.mcp_serena_image_id,
            execution_backend="docker" if args.docker_ordinary_image_id else "local",
            docker_profile="ordinary" if args.docker_ordinary_image_id else "frozen",
            docker_image_id=args.docker_ordinary_image_id,
        )
    )
    mcp_queries = []
    if args.mcp_serena_image_id is not None:
        trace_lines = Path(result.trace_path).read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in trace_lines]
        mcp_queries = [
            event["payload"]["result"] for event in events
            if event.get("event_type") == "tool_returned"
            and event.get("payload", {}).get("result", {}).get("tool_name", "").startswith(
                "mcp_serena_"
            )
        ]
        if len(mcp_queries) != 2 or not all(
            item["success"] and item["output"].get("source_sha256") for item in mcp_queries
        ):
            raise AssertionError("isolated Serena queries did not both succeed")
    regression = None
    validation_verification = None
    if args.regression_example:
        regression = verify_patch(
            Path(result.result_path).parent,
            regression_targets=("tests/test_backward.py",),
        )
    if args.validation_gate_example:
        validation_verification = verify_patch(Path(result.result_path).parent)
        if validation_verification["passed"] is not True:
            raise AssertionError("independent regression verification did not pass")
    report = render_report(Path(result.result_path).parent)
    if args.validation_gate_example:
        if result.validation_gate_status != "passed" or client.calls != 4:
            raise AssertionError(
                "offline regression feedback gate did not complete its repair replay"
            )
    source_clean = not subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True,
        text=True, check=True,
    ).stdout.strip()
    if not source_clean:
        raise AssertionError("ordinary replay modified its source repository")
    print(
        json.dumps(
            {
                "status": result.status.value,
                "model_requests": client.calls,
                "mcp_queries": len(mcp_queries),
                "docker_image_id": args.docker_ordinary_image_id,
                "result": result.result_path,
                "report": str(report),
                "source_clean": source_clean,
                "regression_status": regression["status"] if regression else None,
                "regression_record": regression["record_path"] if regression else None,
                "validation_gate_status": result.validation_gate_status,
                "validation_gate_results": result.validation_gate_results,
                "validation_verification_status": (
                    validation_verification["status"] if validation_verification else None
                ),
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
