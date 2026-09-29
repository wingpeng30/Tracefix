"""Verify and summarize the three zero-cost scripted Agent container runs."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.docker_agent_e2e import (  # noqa: E402
    IMAGES,
    PRIOR_REPLAYS,
    acceptance_signature,
    verify_run_identity,
)
from scripts.docker_reverify import verify_input  # noqa: E402

TASK_IDS = (
    "pytest-dev__pytest-10081",
    "psf__requests-1766",
    "sphinx-doc__sphinx-10449",
)


def summarize(output_root: Path, input_root: Path) -> dict:
    entries = []
    for task_id in TASK_IDS:
        candidates = sorted(
            output_root.glob(f"docker-agent-e2e-{task_id}-*"),
            key=lambda path: path.stat().st_mtime,
        )
        selected = None
        for candidate in reversed(candidates):
            report_path = candidate / "run.json"
            if not report_path.is_file():
                continue
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("status") == "completed":
                selected = (candidate, report)
                break
        if selected is None:
            raise ValueError(f"no completed Agent container run for {task_id}")
        run_dir, report = selected
        staged = verify_input(input_root / task_id, task_id)
        _image_ref, expected_image_id, _test_python = IMAGES[task_id]
        verify_run_identity(
            report,
            {
                "task_id": task_id,
                "image_id": expected_image_id,
                "source_commit": staged["source_commit"],
                "input_manifest_sha256": report_sha(input_root / task_id),
                "recipe_fingerprint": staged["recipe_fingerprint"],
            },
        )
        acceptance = report["independent_acceptance"]
        patch_runs = [item for item in acceptance["results"] if item.get("stage") == "patch"]
        qualifications = [
            item for item in acceptance["results"] if item.get("stage") == "qualification"
        ]
        previous_path = Path(PRIOR_REPLAYS[task_id])
        previous_path = REPO / previous_path
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        previous_patch_runs = [
            item for item in previous["results"] if item.get("stage") == "patch"
        ]
        checks = {
            "model_requests_zero": report.get("model_requests") == 0,
            "test_only_diff_empty": report.get("agent_results", {}).get(
                "test_only_product_diff_empty"
            )
            is True,
            "agent_tests_started": report.get("agent_results", {}).get(
                "baseline_test_process_started"
            )
            is True
            and report.get("agent_results", {}).get("post_patch_test_process_started") is True,
            "agent_smoke_passed": report.get("agent_results", {}).get(
                "post_patch_test_success"
            )
            is True,
            "qualification_repeats_passed": len(qualifications) == 2
            and all(item.get("qualified") for item in qualifications),
            "patch_repeats_passed": len(patch_runs) == 2
            and all(item.get("qualified") for item in patch_runs),
            "patch_repeats_stable": len(patch_runs) == 2
            and acceptance_signature(patch_runs[0]) == acceptance_signature(patch_runs[1]),
            "matches_prior_replay": len(patch_runs) == len(previous_patch_runs) == 2
            and all(
                acceptance_signature(current) == acceptance_signature(old)
                for current, old in zip(patch_runs, previous_patch_runs, strict=True)
            ),
            "agent_extra_file_isolated": report.get(
                "agent_extra_file_absent_from_acceptance"
            )
            is True,
            "source_bundle_manifest_verified": staged["source_commit"]
            == report["source_commit"],
        }
        entries.append(
            {
                "task_id": task_id,
                "run_id": report["run_id"],
                "run_dir": str(run_dir),
                "source_commit": report["source_commit"],
                "image_id": report["image_id"],
                "agent_container_id": report["agent_container_id"],
                "acceptance_container_id": report["acceptance_container_id"],
                "patch_sha256": report["agent_results"]["patch_sha256"],
                "changed_files": report["agent_results"]["changed_files"],
                "agent_test_status": report["agent_results"]["post_patch_test_status"],
                "qualification_repeats": len(qualifications),
                "patch_repeats": len(patch_runs),
                "checks": checks,
                "passed": all(checks.values()),
            }
        )
    return {
        "kind": "verified_zero_cost_docker_agent_e2e_summary",
        "generated_at": datetime.now(UTC).isoformat(),
        "model_requests": 0,
        "formal_experiment": False,
        "holdout_used": False,
        "tasks": entries,
        "all_tasks_passed": len(entries) == len(TASK_IDS)
        and all(item["passed"] for item in entries),
    }


def report_sha(root: Path) -> str:
    import hashlib

    return hashlib.sha256((root / "input-manifest.json").read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--input-root",
        type=Path,
        default=Path("runs/docker-foundation-20260926-v1/inputs-v2"),
    )
    args = parser.parse_args()
    result = summarize(args.output_root, args.input_root)
    destination = args.output_root / "docker-agent-e2e-summary-20260926.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(destination)
    return 0 if result["all_tasks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
