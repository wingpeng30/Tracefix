"""Explicitly authorized, serial live-model acceptance with a durable request ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from tracefix.agent.base import AgentConfig
from tracefix.live_budget import LiveBudgetAdapter
from tracefix.onboarding import doctor
from tracefix.runtime import RunConfig, TraceFixRunner


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _smoke_fixture(root: Path) -> Path:
    repo = root / "smoke-source"
    if repo.exists():
        return repo
    repo.mkdir(parents=True)
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (repo / "widget.py").write_text("def next_page(page):\n    return page\n", encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_widget.py").write_text(
        "from widget import next_page\n\ndef test_next_page():\n"
        "    assert next_page(1) == 2\n", encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "--all"], cwd=repo, check=True)
    subprocess.run([
        "git", "-c", "user.name=TraceFix Live Fixture", "-c",
        "user.email=fixture@example.invalid", "commit", "-qm", "fixture",
    ], cwd=repo, check=True)
    return repo


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--task")
    parser.add_argument("--test-target")
    parser.add_argument("--source-import")
    parser.add_argument("--skills-root", type=Path)
    parser.add_argument("--context-trigger", type=int, default=4800)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    temporary_root = root / "temporary"
    temporary_root.mkdir(exist_ok=True)
    tempfile.tempdir = str(temporary_root)
    manifest_path = root / "runs.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file() else {"schema_version": 1, "max_runs": 8, "runs": []}
    )
    if manifest["schema_version"] != 1 or manifest["max_runs"] != 8:
        parser.error("live experiment run manifest identity changed")
    if len(manifest["runs"]) >= 8 or any(
        run["stage"] == args.stage for run in manifest["runs"]
    ):
        parser.error("run limit reached or this stage was already attempted")
    if any(run["status"] == "started" for run in manifest["runs"]):
        parser.error("a prior run has an unknown result; inspect it before continuing")
    if args.stage == "smoke":
        repo = _smoke_fixture(root)
        task = "修复分页函数，使公开测试通过"
        test_target = "tests/test_widget.py"
        source_import = "widget"
        max_steps, max_input, max_output, per_request, max_tests, wall, timeout = (
            10, 12000, 2000, 512, 2, 300, 45
        )
    else:
        if not all((args.repo, args.task, args.test_target, args.source_import)):
            parser.error("other stages require repo, task, test target and source import")
        repo = args.repo.resolve()
        task, test_target, source_import = args.task, args.test_target, args.source_import
        max_steps, max_input, max_output, per_request, max_tests, wall, timeout = (
            20, 60000, 8000, 2048, 4, 600, 60
        )
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    implementation_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    config = RunConfig(
        repo=repo, task=task, model_name="deepseek/deepseek-flash",
        output_dir=root / "runs", env_file=args.env_file.resolve(),
        llm_max_retries=0, llm_timeout_seconds=timeout,
        per_request_output_tokens=per_request, test_python_executable=Path(sys.executable),
        test_target=test_target, source_import=source_import,
        skills_root=args.skills_root.resolve() if args.skills_root else None,
        agent_config=AgentConfig(
            max_steps=max_steps, max_input_tokens=max_input, max_output_tokens=max_output,
            max_test_runs=max_tests, wall_time_seconds=wall, record_request_views=True,
            skills_enabled=args.skills_root is not None,
            context={"context_window_tokens": 6000 if args.stage == "smoke" else 20000,
                     "compaction_trigger_tokens": args.context_trigger},
        ),
    )
    config_hash = hashlib.sha256(config.model_dump_json().encode("utf-8")).hexdigest()
    preflight = doctor({
        "repo": repo, "test_python_executable": Path(sys.executable),
        "test_target": test_target, "source_import": source_import,
        "skills_root": config.skills_root, "output_dir": config.output_dir,
        "model_name": config.model_name, "env_file": config.env_file,
    }, prepare=True)
    print(json.dumps({
        "preflight": preflight, "source_commit": source_commit,
        "implementation_commit": implementation_commit, "config_sha256": config_hash,
    }, ensure_ascii=False, indent=2))
    if not preflight["ok"]:
        return 2
    if args.prepare_only:
        return 0
    record = {
        "stage": args.stage, "status": "started", "source_commit": source_commit,
        "implementation_commit": implementation_commit, "config_sha256": config_hash,
        "model": config.model_name,
    }
    manifest["runs"].append(record)
    _write_json(manifest_path, manifest)
    adapter = lambda llm_config: LiveBudgetAdapter(  # noqa: E731
        llm_config, ledger_path=root / "requests.json", limit_cny=20.0,
    )
    try:
        result = TraceFixRunner(llm_factory=adapter).run(config)
    except BaseException:
        # The manifest remains started: an unknown result must be reviewed manually.
        raise
    record["status"] = result.status.value
    record["result_path"] = result.result_path
    record["cost_complete"] = result.cost_complete
    record["usage_complete"] = result.usage_complete
    _write_json(manifest_path, manifest)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if result.status.value == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
