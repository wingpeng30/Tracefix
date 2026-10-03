"""Installed CLI dialogue acceptance with recorded responses and real pytest processes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from tracefix import cli
from tracefix.memory import atomic_json
from tracefix.memory_replay import _PATCH, RecordedMemoryClient
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.onboarding import verify_patch
from tracefix.regression_replay import forbid_live_access
from tracefix.reproduction import _git
from tracefix.runtime import RunResult, TraceFixRunner


class DialogueClient(RecordedMemoryClient):
    def __init__(self, turn: int) -> None:
        super().__init__(recall=False)
        before = ("return page", "return page + 1",
                  "return page if page < 0 else page + 1")[turn - 1]
        after = ("return page + 1", "return page if page < 0 else page + 1",
                 "return page if page <= 0 else page + 1")[turn - 1]
        target = ("test_positive", "test_negative", "test_zero")[turn - 1]
        self.sequence = [
            ("run_tests", {"command": f"pytest -q tests/test_widget.py::{target}"}),
            ("read_file", {"path": "widget.py"}),
            ("apply_patch", {"patch": _PATCH.replace("-    return page", f"-    {before}")
                             .replace("+    return page + 1", f"+    {after}")}),
            ("run_tests", {"command": "pytest -q tests/test_widget.py" if turn == 3
                           else f"pytest -q tests/test_widget.py::{target}"}),
            ("get_git_diff", {"context_lines": 3}), None,
        ]


def worker(output: Path, turn: int) -> int:
    client = DialogueClient(turn)
    runner = TraceFixRunner(lambda config: LiteLLMAdapter(config, client=client))
    if turn == 1:
        arguments = ["chat", "--config", str(output / "chat.toml")]
    else:
        root = next((output / "runs").glob("*/session.json")).parent
        arguments = (["chat", "--run", str(root)] if turn == 2 else
                     ["continue", "--run", str(root), "--message", "Preserve zero sentinel"])
    with forbid_live_access(), patch.object(cli, "TraceFixRunner", lambda: runner):
        code = cli.main(arguments)
    atomic_json(output / f"process-{turn}.json", {
        "pid": os.getpid(), "turn": turn, "exit_code": code,
        "provider_calls": 0, "recorded_requests": client.calls,
    })
    return code


def prepare_dialogue(output: Path) -> None:
    """Create a committed source fixture and an ordinary public CLI configuration."""
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    source.mkdir()
    (source / "tests").mkdir()
    (source / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (source / "widget.py").write_text("def next_page(page):\n    return page\n", encoding="utf-8")
    (source / "tests/test_widget.py").write_text(
        "from widget import next_page\n"
        "def test_positive(): assert next_page(1) == 2\n"
        "def test_negative(): assert next_page(-1) == -1\n"
        "def test_zero(): assert next_page(0) == 0\n", encoding="utf-8",
    )
    _git(source, "init")
    _git(source, "add", "--all")
    _git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-m", "dialogue baseline")
    (output / "chat.toml").write_text(
        '[run]\nrepo = "source"\noutput_dir = "runs"\ntask = "Fix pagination"\n'
        'model = "offline/dialogue"\n'
        'source_import = "widget"\n'
        'test_target = "tests/test_widget.py::test_positive"\nmemory = true\n'
        f'test_python = {json.dumps(sys.executable)}\n', encoding="utf-8",
    )
def run_dialogue_replay(output: Path) -> dict:
    output = output.expanduser().resolve()
    prepare_dialogue(output)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
           "PYTHONIOENCODING": "utf-8"}
    for turn, stdin in ((1, ":exit\n"), (2, "Correction: preserve negative sentinel\n:exit\n"),
                        (3, "")):
        process = subprocess.run(
            [sys.executable, "-m", "tracefix.dialogue_replay", "--output", str(output),
             "--turn", str(turn)], input=stdin, text=True, encoding="utf-8", capture_output=True,
            cwd=output, env=env,
        )
        (output / f"process-{turn}.stdout.txt").write_text(process.stdout, encoding="utf-8")
        (output / f"process-{turn}.stderr.txt").write_text(process.stderr, encoding="utf-8")
        if process.returncode:
            raise RuntimeError(f"dialogue turn {turn} failed: {process.stderr[-3000:]}")
    root = next((output / "runs").glob("*/session.json")).parent
    result = RunResult.model_validate_json((root / "result.json").read_text(encoding="utf-8"))
    processes = [json.loads((output / f"process-{turn}.json").read_text(encoding="utf-8"))
                 for turn in (1, 2, 3)]
    if (result.status != "completed" or result.turn_number != 3
            or len(result.turn_records) != 3 or result.step_count != 21
            or len({item["pid"] for item in processes}) != 3
            or not TraceFixRunner.inspect(root).get("continuable")):
        raise AssertionError("three committed, independently entered rounds were not proven")
    receipts = [json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(root.glob("memory-job-turn-*.json"))]
    if len(receipts) != 3 or any(item.get("status") not in {"verified", "duplicate"}
                                 for item in receipts):
        raise AssertionError("each round did not commit one evidence-bound memory receipt")
    with forbid_live_access():
        verification = verify_patch(root, regression_targets=("tests/test_widget.py",))
    if not verification["passed"]:
        raise AssertionError(verification)
    summary = {
        "scenario": "continuous-dialogue", "accepted": True, "provider_calls": 0,
        "usage_is_simulated": True, "processes": processes, "run": str(root),
        "independent_passed": True, "turns": result.turn_records,
        "sha256": {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in root.rglob("memory-job-turn-*.json")},
    }
    atomic_json(output / "reproduction.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--turn", type=int, choices=(1, 2, 3), required=True)
    args = parser.parse_args()
    return worker(args.output, args.turn)


if __name__ == "__main__":
    raise SystemExit(main())
