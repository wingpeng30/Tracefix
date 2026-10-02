"""Real tools and injected recorded adapter client for investigation invariants."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from history_analysis import digest, line_page

from tracefix import AgentConfig, RunConfig, TraceFixRunner
from tracefix.agent.presentation import ToolResultPresenter
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.onboarding import verify_patch
from tracefix.regression_replay import forbid_live_access


def model_tool(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


def product_patch(before: list[str], after: list[str]) -> str:
    return (
        "*** Begin Patch\n*** Update File: widget.py\n@@\n"
        + "\n".join("-" + line for line in before)
        + "\n"
        + "\n".join("+" + line for line in after)
        + "\n*** End Patch\n"
    )


class MicroClient:
    """Outputs depend on visible source and real gate feedback, never live API."""

    def __init__(self):
        self.calls = 0
        self.feedback = False
        self.fresh_read = False
        self.clipping_observed = False

    def completion(self, **kwargs):
        self.calls += 1
        messages = kwargs["messages"]
        outputs = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if self.calls == 3:
            read = next(o["output"] for o in reversed(outputs) if o["tool_name"] == "read_file")
            self.clipping_observed = "def next_page" not in read.get("content", "")
            assert self.clipping_observed, "fixture must expose hidden middle evidence"
        if self.calls == 4:
            assert any("def next_page" in str(o.get("output")) for o in outputs)
        if self.calls == 6:
            read = next(o for o in reversed(outputs) if o["tool_name"] == "read_file")
            self.fresh_read = "return page + 1" in str(read.get("output"))
            assert self.fresh_read, "patch must invalidate prior cached source"
        if self.calls == 7:
            self.feedback = any(
                "自动验收未通过" in (m.get("content") or "")
                and "test_backward" in (m.get("content") or "")
                for m in messages
            )
            assert self.feedback, "real regression gate feedback must remain visible"
        operations = {
            1: ("search_code", {"query": "def next_page", "path": "widget.py"}),
            2: ("read_file", {"path": "widget.py"}),
            3: ("read_file", {"path": "widget.py", "start_line": 140, "end_line": 145}),
            4: (
                "apply_patch",
                {
                    "patch": product_patch(
                        ["def next_page(page):", "    return page"],
                        ["def next_page(page):", "    return page + 1"],
                    )
                },
            ),
            5: ("read_file", {"path": "widget.py", "start_line": 140, "end_line": 145}),
            7: (
                "apply_patch",
                {
                    "patch": product_patch(
                        ["def next_page(page):", "    return page + 1"],
                        [
                            "def next_page(page):",
                            "    if page < 0:",
                            "        return page",
                            "    return page + 1",
                        ],
                    )
                },
            ),
            8: ("run_tests", {"command": "pytest -q tests/test_widget.py"}),
            9: ("get_git_diff", {}),
        }
        assert self.calls <= 10
        calls = (
            [model_tool(f"micro-{self.calls}", *operations[self.calls])]
            if self.calls in operations
            else []
        )
        return {
            "model": "offline/micro",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
            "choices": [
                {
                    "message": {"content": "done" if not calls else None, "tool_calls": calls},
                    "finish_reason": "stop",
                }
            ],
        }


def run_micro(output: Path, python: Path, variant: str) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    source.mkdir()
    (source / "tests").mkdir()
    code = [f"# filler {i:03d}: " + "deterministic source context " * 3 for i in range(1, 140)]
    code += ["def next_page(page):", "    return page"]
    code += [f"# tail {i:03d}: " + "other source context " * 3 for i in range(142, 221)]
    (source / "widget.py").write_text("\n".join(code) + "\n", encoding="utf-8")
    (source / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (source / "tests/test_widget.py").write_text(
        "from widget import next_page\ndef test_page(): assert next_page(1) == 2\n",
        encoding="utf-8",
    )
    (source / "tests/test_backward.py").write_text(
        "from widget import next_page\ndef test_backward(): assert next_page(-1) == -1\n",
        encoding="utf-8",
    )
    for args in (
        ["init", "-q"],
        ["add", "--all"],
        [
            "-c",
            "user.name=TraceFix micro",
            "-c",
            "user.email=micro@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "offline micro fixture",
        ],
    ):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
    original = digest((source / "widget.py").read_bytes())
    client = MicroClient()
    presenter = ToolResultPresenter.present

    def experimental_present(self, result):
        if variant == "line_pages" and result.tool_name == "read_file" and result.success:
            return json.dumps(line_page(result), ensure_ascii=False, separators=(",", ":"))
        return presenter(self, result)

    with forbid_live_access(), patch.object(ToolResultPresenter, "present", experimental_present):
        result = TraceFixRunner(
            llm_factory=lambda config: LiteLLMAdapter(config, client=client)
        ).run(
            RunConfig(
                repo=source,
                task="Fix next_page; preserve negative sentinel. Do not modify tests.",
                model_name="offline/micro",
                env_file=None,
                output_dir=output / "runs",
                test_python_executable=python,
                source_import="widget",
                test_target="tests/test_widget.py",
                regression_targets=["tests/test_backward.py"],
                agent_config=AgentConfig(
                    max_steps=12,
                    max_test_runs=8,
                    max_input_tokens=60000,
                    max_output_tokens=8000,
                    wall_time_seconds=300,
                    record_request_views=True,
                    context={
                        "context_window_tokens": 40000,
                        "compaction_trigger_tokens": 3000 if variant == "context" else 32000,
                    },
                ),
            )
        )
        verification = verify_patch(Path(result.result_path).parent)
    events = [
        json.loads(line)
        for line in (Path(result.result_path).parent / "trajectory.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    record = {
        "kind": "scripted_control_flow_not_model_repair_success",
        "usage_kind": "simulated",
        "supplier_requests": 0,
        "variant": variant,
        "status": result.status.value,
        "gate": result.validation_gate_status,
        "independent_passed": verification["passed"],
        "result_path": result.result_path,
        "feedback_observed": client.feedback,
        "fresh_read_observed": client.fresh_read,
        "clipping_observed": client.clipping_observed,
        "compactions": sum(
            e["event_type"] == "context_prepared" and e["payload"]["compacted"] for e in events
        ),
        "source_unchanged": original == digest((source / "widget.py").read_bytes()),
        "test_runs": result.test_runs,
    }
    (output / "micro.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    if result.status.value != "completed" or not verification["passed"] or not client.feedback:
        raise ValueError(f"micro verification failed: {result.result_path}")
    return record
