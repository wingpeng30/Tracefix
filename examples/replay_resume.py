"""Offline replay of a committed-batch interruption and local session resume."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from replay_ordinary import ReplayClient

from tracefix.checkpoint import CheckpointStore
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.onboarding import export_patch
from tracefix.report import render_report
from tracefix.runtime import RunConfig, TraceFixRunner


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if output.exists():
        parser.error("output must not already exist")
    source = output / "source"
    source.mkdir(parents=True)
    (source / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (source / "widget.py").write_text("def next_page(page):\n    return page\n", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "test_widget.py").write_text(
        "from widget import next_page\n\ndef test_next_page():\n    assert next_page(1) == 2\n",
        encoding="utf-8",
    )
    for command in (
        ("init", "-q"), ("add", "--all"),
        ("-c", "user.name=Replay", "-c", "user.email=replay@example.invalid",
         "commit", "-qm", "fixture"),
    ):
        subprocess.run(["git", *command], cwd=source, check=True, capture_output=True)
    client = ReplayClient()
    runner = TraceFixRunner(llm_factory=lambda config: LiteLLMAdapter(config, client=client))
    original_save = CheckpointStore.save
    paused = False

    def pause_once(self, payload, *, sequence, pending_calls=()):
        nonlocal paused
        saved = original_save(self, payload, sequence=sequence, pending_calls=pending_calls)
        if sequence == 3 and not paused:
            paused = True
            raise KeyboardInterrupt
        return saved

    CheckpointStore.save = pause_once
    try:
        first = runner.run(RunConfig(
            repo=source, task="修复分页测试", model_name="offline/replay",
            output_dir=output / "runs", env_file=None,
            test_python_executable=Path(sys.executable),
            test_target="tests/test_widget.py", source_import="widget",
        ))
    finally:
        CheckpointStore.save = original_save
    run_dir = Path(first.result_path).parent
    inspection = TraceFixRunner.inspect(run_dir)
    if first.status.value != "interrupted" or not inspection["resumable"]:
        raise RuntimeError(f"offline interruption was not resumable: {inspection}")
    final = runner.resume(run_dir)
    if final.status.value != "completed" or final.agent_validation_status != "verified":
        raise RuntimeError("offline recovery did not complete a verified patch")
    report = render_report(run_dir)
    exported = export_patch(run_dir, output / "fix.patch")
    source_status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=source,
        check=True, capture_output=True, text=True,
    ).stdout
    if source_status:
        raise RuntimeError("source repository changed during recovery")
    print(json.dumps({
        "first_status": first.status.value,
        "final_status": final.status.value,
        "validation": final.agent_validation_status,
        "model_requests": client.calls,
        "run": str(run_dir),
        "report": str(report),
        "patch": str(exported),
        "source_clean": True,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
