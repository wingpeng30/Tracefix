"""Package-only, recorded-response validation-gate reproduction (no live model)."""

from __future__ import annotations

import builtins
import hashlib
import importlib
import json
import socket
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from tracefix import AgentConfig, RunConfig, TraceFixRunner
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.onboarding import export_patch, verify_patch
from tracefix.report import render_report

FIRST_PATCH = """diff --git a/widget.py b/widget.py
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
REPAIR_PATCH = """diff --git a/widget.py b/widget.py
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


class RecordedRegressionClient:
    """Feed serialized responses through the production adapter, checking feedback."""

    def __init__(self) -> None:
        self.calls = 0
        self.feedback_observed = False

    def token_counter(self, **_kwargs):
        return 100

    def completion(self, **kwargs):
        self.calls += 1
        if not kwargs.get("tools") or not kwargs.get("messages"):
            raise AssertionError("production tool schemas and messages are required")
        if self.calls == 3:
            messages = kwargs["messages"]
            self.feedback_observed = any(
                message.get("role") == "user"
                and "自动验收未通过" in str(message.get("content"))
                and "tests/test_backward.py" in str(message.get("content"))
                and "tests/test_identity.py" in str(message.get("content"))
                and "regression" in str(message.get("content"))
                for message in messages
            )
            if not self.feedback_observed:
                raise AssertionError("regression failure feedback did not reach the model request")
        if self.calls > 4:
            raise AssertionError("unexpected additional recorded model request")
        product_patch = {1: FIRST_PATCH, 3: REPAIR_PATCH}.get(self.calls)
        calls = (
            []
            if product_patch is None
            else [
                {
                    "id": f"recorded-{self.calls}",
                    "type": "function",
                    "function": {
                        "name": "apply_patch",
                        "arguments": json.dumps({"patch": product_patch}),
                    },
                }
            ]
        )
        return {
            "model": "offline/regression-feedback",
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None if calls else "Recorded repair completed",
                        "tool_calls": calls,
                    },
                    "finish_reason": "tool_calls" if calls else "stop",
                }
            ],
        }


@contextmanager
def forbid_live_access():
    """Keep adapter serialization/parsing live, forbid lazy provider import and sockets."""
    original_import = importlib.import_module
    original_builtin_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "litellm" or name.startswith("litellm."):
            raise AssertionError("provider import is forbidden in recorded reproduction")
        return original_import(name, *args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("live provider/network access is forbidden in recorded reproduction")

    def guarded_builtin_import(name, *args, **kwargs):
        if name == "litellm" or name.startswith("litellm."):
            raise AssertionError("provider import is forbidden in recorded reproduction")
        return original_builtin_import(name, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch("importlib.import_module", guarded_import))
        stack.enter_context(patch("builtins.__import__", guarded_builtin_import))
        stack.enter_context(patch("tracefix.runtime.LiteLLMAdapter", forbidden))
        stack.enter_context(patch.object(socket, "create_connection", forbidden))
        stack.enter_context(patch.object(socket.socket, "connect", forbidden))
        stack.enter_context(patch.object(socket.socket, "connect_ex", forbidden))
        yield


def run_regression_feedback(output: Path) -> dict:
    from tracefix.cli import _ordinary_settings, build_parser
    from tracefix.reproduction import _git

    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    source.mkdir()
    (source / "tests").mkdir()
    files = {
        ".gitignore": "__pycache__/\n.pytest_cache/\n",
        "widget.py": "def next_page(page):\n    return page\n",
        "identity.py": "def same(value): return value\n",
        "tests/test_widget.py": (
            "from widget import next_page\n\ndef test_page():\n    assert next_page(1) == 2\n"
        ),
        "tests/test_backward.py": (
            "from widget import next_page\n\ndef test_sentinel():\n    assert next_page(-1) == -1\n"
        ),
        "tests/test_identity.py": (
            "from identity import same\n\ndef test_identity():\n    assert same(7) == 7\n"
        ),
    }
    for name, content in files.items():
        (source / name).write_text(content, encoding="utf-8")
    _git(source, "init")
    _git(source, "add", *files)
    _git(source, "config", "user.name", "TraceFix fixture")
    _git(source, "config", "user.email", "fixture@example.invalid")
    _git(source, "commit", "-m", "Recorded regression-feedback fixture")
    config_file = output / "config.toml"
    config_file.write_text(
        '[run]\nrepo = "source"\ntask = "修复分页，保留负数哨兵及身份函数"\n'
        'model = "offline/regression-feedback"\noutput_dir = "runs"\n'
        'test_target = "tests/test_widget.py"\nsource_import = "widget"\n'
        'regression_targets = ["tests/test_backward.py", "tests/test_identity.py"]\n'
        "max_steps = 6\nmax_test_runs = 8\nwall_time_seconds = 300\n",
        encoding="utf-8",
    )
    settings = _ordinary_settings(
        build_parser().parse_args(
            [
                "doctor",
                "--config",
                str(config_file),
                "--test-python",
                sys.executable,
                "--repo",
                str(source),
                "--model",
                "offline/regression-feedback",
                "--output-dir",
                str(output / "runs"),
                "--test-target",
                "tests/test_widget.py",
                "--source-import",
                "widget",
            ]
        )
    )
    client = RecordedRegressionClient()
    with forbid_live_access():
        result = TraceFixRunner(
            llm_factory=lambda config: LiteLLMAdapter(config, client=client),
        ).run(
            RunConfig(
                repo=settings["repo"],
                task=settings["task"],
                model_name=settings["model_name"],
                output_dir=settings["output_dir"],
                env_file=None,
                test_python_executable=Path(sys.executable),
                test_target=settings["test_target"],
                source_import=settings["source_import"],
                regression_targets=settings["regression_targets"],
                agent_config=AgentConfig(max_steps=6, max_test_runs=8, wall_time_seconds=300),
            )
        )
        run = Path(result.result_path).parent
        if (
            result.status.value != "completed"
            or result.validation_gate_status != "passed"
            or result.test_runs != 8
            or client.calls != 4
            or not client.feedback_observed
        ):
            raise AssertionError(f"recorded regression repair failed: {result.status}, {run}")
        verification = verify_patch(run)
        if verification.get("passed") is not True:
            raise AssertionError(f"independent verification failed: {verification}")
        report_path = render_report(run, output / "report.html")
        exported = export_patch(run, output / "export.patch")
    source_clean = not subprocess.check_output(
        ["git", "status", "--porcelain"],
        cwd=source,
        text=True,
    ).strip()
    if not source_clean:
        raise AssertionError("source repository changed")
    summary = {
        "kind": "recorded_response_harness_reproduction",
        "scenario": "regression-feedback",
        "status": result.status.value,
        "run_path": str(run),
        "source_path": str(source),
        "source_commit": result.source_commit,
        "source_clean": source_clean,
        "model_requests": client.calls,
        "feedback_observed": client.feedback_observed,
        "usage_kind": "simulated_recorded_usage",
        "provider_cost": None,
        "validation_gate_status": result.validation_gate_status,
        "test_runs": result.test_runs,
        "independent_verification_status": verification["status"],
        "independent_record_path": verification["record_path"],
        "report_path": str(report_path),
        "export_path": str(exported),
        "artifact_sha256": {
            str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                Path(result.result_path),
                Path(result.trace_path),
                Path(result.diff_path),
                report_path,
                exported,
                config_file,
                Path(verification["record_path"]),
                exported.with_name(exported.name + ".sha256"),
            )
        },
    }
    (output / "reproduction.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary
