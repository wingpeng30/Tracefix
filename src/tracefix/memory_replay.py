"""Installed-package, separate-process experience learning and recall reproduction."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from tracefix.memory import ExperienceProposal, ExperienceStore, atomic_json
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.onboarding import verify_patch
from tracefix.regression_replay import forbid_live_access
from tracefix.reproduction import _git
from tracefix.runtime import RunConfig, TraceFixRunner

_PATCH = """diff --git a/widget.py b/widget.py
--- a/widget.py
+++ b/widget.py
@@ -1,2 +1,2 @@
 def next_page(page):
-    return page
+    return page + 1
"""


class RecordedMemoryClient:
    """A conditional fixture: recall must actually reach the model before repository tools run."""

    def __init__(self, *, recall: bool) -> None:
        self.calls = 0
        self.recalled = False
        self.recall = recall
        self.sequence = ([
            ("load_skill", {"name": "experience-pagination"}),
        ] if recall else []) + [
            ("run_tests", {"command": "pytest -q tests/test_widget.py"}),
            ("read_file", {"path": "widget.py"}),
            ("apply_patch", {"patch": _PATCH}),
            ("run_tests", {"command": "pytest -q tests/test_widget.py"}),
            ("get_git_diff", {"context_lines": 3}),
            None,
        ]

    def token_counter(self, **kwargs):
        return 100

    def completion_cost(self, **kwargs):
        return 0.0

    def completion(self, **kwargs):
        self.calls += 1
        messages = kwargs["messages"]
        content = None
        calls = []
        if not kwargs.get("tools"):
            data = json.loads(messages[-1]["content"])
            results = data["evidence"]["tool_results"]
            read_id = next(key for key, result in results.items()
                           if result["tool_name"] == "read_file" and result["success"])
            content = ExperienceProposal(
                key="pagination", summary="An observed pagination inspection workflow",
                keywords=["pagination"], applicability_paths=["widget.py"],
                steps=[{"instruction": "Read the current implementation before editing.",
                        "call_id": read_id}],
                test_call_id=data["evidence"]["latest_test"]["call_id"], generalized=False,
            ).model_dump_json()
        else:
            if self.recall and self.calls > 1:
                self.recalled = any(
                    "<tracefix_skill_instructions>" in str(message.get("content"))
                    and "Read the current implementation before editing." in
                    str(message.get("content")) for message in messages
                )
                if not self.recalled:
                    raise AssertionError("saved experience did not reach the model request")
            call = self.sequence[self.calls - 1]
            if call is None:
                content = "Recorded pagination repair completed"
            else:
                calls = [{"id": f"memory-replay-{self.calls}", "type": "function",
                          "function": {"name": call[0], "arguments": json.dumps(call[1])}}]
        return {
            "model": "offline/memory-experience",
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            "choices": [{"message": {"role": "assistant", "content": content,
                                     "tool_calls": calls},
                         "finish_reason": "tool_calls" if calls else "stop"}],
        }


def worker(output: Path, phase: str) -> dict:
    source = output / "source"
    if phase == "learn":
        source.mkdir()
        (source / "tests").mkdir()
        (source / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
        (source / "widget.py").write_text(
            "def next_page(page):\n    return page\n", encoding="utf-8",
        )
        (source / "tests/test_widget.py").write_text(
            "from widget import next_page\n\ndef test_next_page():\n    assert next_page(1) == 2\n",
            encoding="utf-8",
        )
        _git(source, "init")
        _git(source, "add", "--all")
        _git(source, "-c", "user.name=TraceFix fixture", "-c", "user.email=fixture@example.invalid",
             "commit", "-m", "Recorded memory source")
    client = RecordedMemoryClient(recall=phase == "recall")
    config = RunConfig(
        repo=source, task="Fix pagination", output_dir=output / "runs",
        model_name="offline/memory-experience", env_file=None, memory_enabled=True,
        test_target="tests/test_widget.py", source_import="widget",
        test_python_executable=Path(sys.executable),
    )
    with forbid_live_access():
        result = TraceFixRunner(llm_factory=lambda parameters: LiteLLMAdapter(
            parameters, client=client,
        )).run(config)
        if result.status != "completed" or result.agent_validation_status != "verified":
            raise AssertionError(result.model_dump(mode="json"))
        if result.memory_status.get("status") not in {"verified", "duplicate"}:
            raise AssertionError(result.memory_status)
        if result.step_count != client.calls or result.input_tokens != 100 * client.calls:
            raise AssertionError("extraction was not included in cumulative accounting")
        independent = verify_patch(Path(result.result_path).parent)
        if not independent["passed"]:
            raise AssertionError(independent)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=source,
                                check=True, capture_output=True, text=True).stdout
        if status.strip():
            raise AssertionError("source repository was changed")
    summary = {"phase": phase, "pid": os.getpid(), "provider_calls": 0,
               "recorded_requests": client.calls, "usage_is_simulated": True,
               "run": str(Path(result.result_path).parent), "recalled": client.recalled,
               "memory": result.memory_status, "independent_passed": independent["passed"]}
    atomic_json(output / f"{phase}.json", summary)
    return summary


def run_memory_replay(output: Path) -> dict:
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    # This points to the running package, including site-packages in a wheel installation.
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    for phase in ("learn", "recall"):
        run = subprocess.run(
            [sys.executable, "-m", "tracefix.memory_replay", "--output", str(output),
             "--phase", phase], cwd=output, env=env, capture_output=True, text=True,
        )
        (output / f"{phase}.stdout.txt").write_text(run.stdout, encoding="utf-8")
        (output / f"{phase}.stderr.txt").write_text(run.stderr, encoding="utf-8")
        if run.returncode:
            raise RuntimeError(f"{phase} failed ({run.returncode}): {run.stderr[-4000:]}")
    learned = json.loads((output / "learn.json").read_text(encoding="utf-8"))
    recalled = json.loads((output / "recall.json").read_text(encoding="utf-8"))
    if learned["pid"] == recalled["pid"] or not recalled["recalled"]:
        raise AssertionError("cross-process recall was not proven")
    store = ExperienceStore(output / "runs/memory", output / "source")
    store.disable("pagination")
    if store.select("pagination"):
        raise AssertionError("disabled memory was still selected")
    store.rollback("pagination", 1)
    if not store.select("pagination"):
        raise AssertionError("verified rollback did not reactivate memory")
    from tracefix.report import render_report

    for run in (learned, recalled):
        render_report(Path(run["run"]))
    summary = {
        "scenario": "memory-experience", "accepted": True, "provider_calls": 0,
        "usage_is_simulated": True, "learn": learned, "recall": recalled,
        "disable_and_rollback": True,
        "sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in output.glob("*.json")},
    }
    atomic_json(output / "reproduction.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=("learn", "recall"), required=True)
    args = parser.parse_args()
    print(json.dumps(worker(args.output, args.phase), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
