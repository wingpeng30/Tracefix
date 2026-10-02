"""Fresh five-run/CNY-five campaign over qualified public task contracts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from live_acceptance import _write_json
from qualify_public_task import TASKS, git, load_manifest, sha, source_state

from tracefix import AgentConfig, RunConfig, TraceFixRunner
from tracefix.checkpoint import ProcessLock
from tracefix.live_budget import LiveBudgetAdapter
from tracefix.onboarding import doctor, export_patch, verify_patch
from tracefix.provenance import inspect_test_environment
from tracefix.report import render_report

CAMPAIGN = "2026-10-02-public-three-types-5runs-5cny"


def read_ledger(path: Path, task: str, stage: str) -> dict:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "campaign": CAMPAIGN, "max_runs": 5, "limit_cny": 5.0, "runs": [],
    }
    if (data.get("campaign"), data.get("max_runs"), data.get("limit_cny")) != (
        CAMPAIGN, 5, 5.0,
    ):
        raise ValueError("campaign identity changed; do not reuse an old ledger")
    runs = data["runs"]
    if len(runs) >= 5 or any(r["stage"] == stage for r in runs):
        raise ValueError("five-run limit reached or duplicate stage")
    if any(r["status"] == "started" or not r.get("usage_complete", False) for r in runs):
        raise ValueError("unknown prior run/usage; stop paid calls")
    if len(runs) < 3:
        if task != list(TASKS)[len(runs)] or stage != task:
            raise ValueError("first three runs must follow the frozen task order")
    elif not stage.startswith("recheck-"):
        raise ValueError("remaining runs are targeted rechecks only")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--task", choices=sorted(TASKS), required=True)
    parser.add_argument("--stage")
    parser.add_argument("--recheck-reason")
    parser.add_argument("--qualification", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with ProcessLock(root):
        return execute(args, root)


def execute(args, root: Path) -> int:
    task = args.task
    stage = args.stage or task
    ledger_path = root / "runs.json"
    ledger = read_ledger(ledger_path, task, stage)
    if stage.startswith("recheck-") and not args.recheck_reason:
        raise ValueError("targeted recheck requires a recorded reproduced issue and fix")
    package = TASKS[task]
    manifest = load_manifest(package)
    qualification = json.loads(args.qualification.read_text(encoding="utf-8"))
    if not qualification.get("qualified") or qualification["task_id"] != task:
        raise ValueError("task is not qualified")
    if qualification["task_manifest_sha256"] != sha(package / "manifest.json"):
        raise ValueError("qualified manifest changed")
    repo = args.qualification.resolve().parent / "prepared-source"
    original = source_state(repo)
    if original != qualification["prepared_source"] or git(repo, "status", "--porcelain").strip():
        raise ValueError("prepared task source changed")
    python = Path(qualification["test_python"])
    if inspect_test_environment(python).fingerprint_sha256 != qualification["environment"][
        "fingerprint_sha256"
    ]:
        raise ValueError("qualified test environment changed")
    implementation = Path(__file__).resolve().parents[1]
    if git(implementation, "status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("paid campaign requires a clean tracked implementation")
    price_path = root / "prices.json"
    price = json.loads(price_path.read_text(encoding="utf-8"))
    if (price["model"], price["input_peak_cny"], price["output_peak_cny"]) != (
        "deepseek/deepseek-flash", 2.0, 8.0,
    ):
        raise ValueError("price snapshot differs from request reservation")
    config = RunConfig(
        repo=repo, task=(package / "task.md").read_text(encoding="utf-8"),
        model_name=price["model"], output_dir=root / "runs", env_file=args.env_file.resolve(),
        llm_max_retries=0, llm_timeout_seconds=60, per_request_output_tokens=2048,
        test_target=manifest["task_target"], source_import=manifest["source_import"],
        regression_targets=manifest["regression_targets"], test_python_executable=python,
        agent_config=AgentConfig(
            max_steps=20, max_input_tokens=60000, max_output_tokens=8000,
            max_test_runs=16, wall_time_seconds=900, record_request_views=True,
            context={"context_window_tokens": 100000, "compaction_trigger_tokens": 32000},
        ),
    )
    preflight = doctor({
        "repo": repo, "test_python_executable": python,
        "test_target": config.test_target, "source_import": config.source_import,
        "regression_targets": config.regression_targets, "output_dir": config.output_dir,
        "model_name": config.model_name, "env_file": config.env_file,
    }, prepare=True)
    stage_dir = root / ("preflight-" + stage if args.prepare_only else stage)
    if stage_dir.exists():
        raise ValueError("stage output already exists")
    stage_dir.mkdir()
    _write_json(stage_dir / "preflight.json", preflight)
    if not preflight["ok"]:
        return 2
    if args.prepare_only:
        print(json.dumps({"prepared": True, "preflight": str(stage_dir / "preflight.json")}))
        # Prepare evidence stays separate from paid attempt output.
        return 0
    record = {
        "stage": stage, "task_id": task, "status": "started",
        "implementation_commit": git(implementation, "rev-parse", "HEAD").strip(),
        "script_sha256": sha(Path(__file__)), "manifest_sha256": sha(package / "manifest.json"),
        "qualification_sha256": sha(args.qualification), "source": original,
        "price_snapshot_sha256": sha(price_path), "model": config.model_name,
        "recheck_reason": args.recheck_reason,
    }
    config_path = stage_dir / "config.json"
    _write_json(config_path, config.model_dump(mode="json"))
    record["config_sha256"] = sha(config_path)
    ledger["runs"].append(record)
    _write_json(ledger_path, ledger)
    runner = TraceFixRunner(llm_factory=lambda llm: LiveBudgetAdapter(
        llm, ledger_path=root / "requests.json", limit_cny=5.0,
    ))
    result = runner.run(config)
    record.update({
        "status": result.status.value, "usage_complete": result.usage_complete,
        "cost_complete": result.cost_complete, "result_path": result.result_path,
        "validation_gate_status": result.validation_gate_status,
    })
    # Persist model outcome before any independent postprocessing can fail.
    _write_json(ledger_path, ledger)
    run = Path(result.result_path).parent
    if (run / "patch.diff").is_file() and (run / "patch.diff").stat().st_size:
        verification = verify_patch(run)
        record["independent_verification"] = verification
        record["export"] = str(export_patch(run, stage_dir / "patch.diff"))
    record["report"] = str(render_report(run, stage_dir / "report.html"))
    record["source_unchanged"] = source_state(repo) == original
    record["artifacts_sha256"] = {
        p.name: sha(p) for p in stage_dir.iterdir() if p.is_file()
    }
    _write_json(ledger_path, ledger)
    print(json.dumps({k: v for k, v in record.items() if k != "source"}, indent=2))
    return 0 if result.validation_gate_status == "passed" and record.get(
        "independent_verification", {}
    ).get("passed") and record["source_unchanged"] else 1


if __name__ == "__main__":
    sys.exit(main())
