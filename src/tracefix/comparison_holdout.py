"""Rules unique to the preregistered holdout comparison; historical rules stay frozen."""

from __future__ import annotations

import itertools
import os
import random
import statistics
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

SEED = 20261006
PRODUCT_BASE = "e380329d2a007a83dd317944bafd426d4b829c82"
HOLDOUT_TASK_IDS = (
    "sphinx-doc__sphinx-8056",
    "sphinx-doc__sphinx-8551",
    "sphinx-doc__sphinx-8593",
    "pytest-dev__pytest-7205",
    "sphinx-doc__sphinx-7889",
    "psf__requests-5414",
    "pytest-dev__pytest-6197",
    "sphinx-doc__sphinx-8120",
    "pylint-dev__pylint-6903",
    "pytest-dev__pytest-5262",
    "pylint-dev__pylint-7277",
    "psf__requests-6028",
    "psf__requests-1921",
    "pytest-dev__pytest-7432",
    "pytest-dev__pytest-7324",
    "pylint-dev__pylint-6528",
    "pylint-dev__pylint-4970",
    "pylint-dev__pylint-6386",
    "psf__requests-2317",
    "psf__requests-2931",
)
FREEZE = (
    Path(__file__).resolve().parents[2]
    / "benchmarks/experiments/v0.8.9-holdout/holdout-freeze.json"
)


def product_diff(workspace: Path, protected_dirs: set[str]) -> tuple[str, tuple[str, ...]]:
    """Stage only in a disposable alternate index; preserve binary and untracked files."""
    with tempfile.TemporaryDirectory(prefix="tracefix-comparison-index-") as root:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(root) / "index")}

        def git(*args):
            return subprocess.check_output(["git", *args], cwd=workspace, env=env)

        git("read-tree", "HEAD")
        exclusions = [f":(exclude){name}/**" for name in sorted(protected_dirs)]
        git("add", "--all", "--", ".", *exclusions)
        changed = tuple(
            n
            for n in git("diff", "--cached", "--name-only", "-z", "HEAD")
            .decode("utf-8")
            .split("\0")
            if n
        )
        for name in changed:
            path = workspace / name
            if not path.resolve().is_relative_to(workspace.resolve()):
                raise ValueError("product path escapes workspace")
        return git("diff", "--cached", "--binary", "HEAD").decode("utf-8"), changed


def balanced_schedule(task_ids: list[str]) -> list[dict]:
    if len(task_ids) != 20 or len(set(task_ids)) != 20:
        raise ValueError("exactly twenty distinct holdout tasks required")
    rng = random.Random(SEED)
    blocks = [(task, repetition) for task in task_ids for repetition in (1, 2, 3)]
    orders = list(itertools.permutations("ABC")) * 10
    rng.shuffle(blocks)
    rng.shuffle(orders)
    rows = []
    for block, ((task, repetition), order) in enumerate(zip(blocks, orders, strict=True), 1):
        for arm in order:
            rows.append(
                {
                    "id": f"{len(rows) + 1:03d}",
                    "task_id": task,
                    "repetition": repetition,
                    "arm": arm,
                    "block": block,
                }
            )
    return rows


def main_success(record: dict) -> bool:
    return record.get("status") == "completed" and bool(record.get("passed"))


def aggregate(records: list[dict], requests: list[dict]) -> dict:
    ids = {r["id"] for r in records}
    usage = [r for r in requests if r["trial_id"] in ids and "usage" in r]
    unknown = [r for r in requests if r["trial_id"] in ids and "usage" not in r]
    successes = sum(main_success(r) for r in records)
    cost = sum(r["peak_cost_cny"] for r in usage)
    seconds = sum(
        r.get("timings", {}).get("delivery_seconds", r.get("seconds", 0)) for r in records
    )
    failures = Counter()
    for r in records:
        if not main_success(r):
            failures[
                (r.get("verification") or {}).get("reason")
                or r.get("reason")
                or r.get("status")
                or "unfinished"
            ] += 1
    raw_usage = [r.get("supplier_raw_usage", {}) for r in usage]
    cache_known = bool(raw_usage) and all(
        "prompt_cache_hit_tokens" in r and "prompt_cache_miss_tokens" in r for r in raw_usage
    )
    cache_hits = sum(r["prompt_cache_hit_tokens"] for r in raw_usage) if cache_known else None
    cache_misses = sum(r["prompt_cache_miss_tokens"] for r in raw_usage) if cache_known else None
    return {
        "started": len(records),
        "successes": successes,
        "success_rate": successes / len(records) if records else None,
        "completed_and_passed": successes,
        "independent_patch_passes": sum(bool(r.get("passed")) for r in records),
        "normal_completions": sum(r.get("status") == "completed" for r in records),
        "conservative_peak_cny": cost,
        "cost_per_success_cny": cost / successes if successes and not unknown else None,
        "cost_complete": not unknown,
        "reserved_unknown_cny": sum(r["reserved_peak_cny"] for r in unknown),
        "seconds": seconds,
        "seconds_per_success": seconds / successes if successes else None,
        "median_seconds": statistics.median(
            [r.get("timings", {}).get("delivery_seconds", r.get("seconds", 0)) for r in records]
        )
        if records
        else None,
        "stage_seconds": {
            stage: sum(r.get("timings", {}).get(stage, 0) for r in records)
            for stage in (
                "preparation_seconds",
                "provider_seconds",
                "counter_seconds",
                "tools_seconds",
                "patch_seconds",
                "verification_seconds",
            )
        },
        "input_tokens": sum(r["usage"]["input_tokens"] for r in usage),
        "output_tokens": sum(r["usage"]["output_tokens"] for r in usage),
        "cache_hit_tokens": cache_hits,
        "cache_miss_tokens": cache_misses,
        "cache_adjusted_peak_estimate_cny": (
            (
                cache_hits * 0.04
                + cache_misses * 2
                + sum(r["usage"]["output_tokens"] for r in usage) * 8
            )
            / 1_000_000
            if cache_known
            else None
        ),
        "supplier_invoice_cny": None,
        "infrastructure_failures": sum(bool(r.get("infrastructure_failure")) for r in records),
        "failure_reasons": dict(failures),
    }


def paired_statistics(rows: list[dict], baseline: str, repetitions: int = 3) -> dict:
    # Only completed three-repetition task pairs estimate the frozen full-task contrast.
    # Started failures still remain in unconditional arm totals; incomplete tasks are explicit.
    matched = [
        r
        for r in rows
        if all(
            r[a]["started"] == repetitions and r[a]["finished"] == repetitions
            for a in (baseline, "C")
        )
    ]
    if not matched:
        return {"matched_tasks": 0, "difference": None, "excluded_incomplete_tasks": len(rows)}

    def contrast(sample):
        c = sum(r["C"]["successes"] for r in sample)
        b = sum(r[baseline]["successes"] for r in sample)
        c_cost = sum(r["C"]["conservative_peak_cny"] for r in sample)
        b_cost = sum(r[baseline]["conservative_peak_cny"] for r in sample)
        return (
            (c - b) / (len(sample) * repetitions) * 100,
            (c_cost / c) / (b_cost / b) if c and b and b_cost else None,
            sum(r["C"]["seconds"] - r[baseline]["seconds"] for r in sample)
            / (len(sample) * repetitions),
        )

    point = contrast(matched)
    rng = random.Random(SEED)
    samples = [contrast(rng.choices(matched, k=len(matched))) for _ in range(10000)]
    intervals = []
    for index in range(3):
        values = sorted(v[index] for v in samples if v[index] is not None)
        # Undefined ratios are not discarded to manufacture a finite confidence interval.
        intervals.append([values[249], values[9749]] if len(values) == 10000 else None)
    deltas = [r["C"]["successes"] - r[baseline]["successes"] for r in matched]
    return {
        "matched_tasks": len(matched),
        "excluded_incomplete_tasks": len(rows) - len(matched),
        "percentage_point_difference": point[0],
        "bootstrap_95_percent_points": intervals[0],
        "cost_per_success_ratio": point[1],
        "bootstrap_95_cost_ratio": intervals[1],
        "seconds_difference": point[2],
        "bootstrap_95_seconds_difference": intervals[2],
        "wins": sum(v > 0 for v in deltas),
        "losses": sum(v < 0 for v in deltas),
        "ties": sum(v == 0 for v in deltas),
    }


def holdout_summary(protocol: dict, records: list[dict], requests: list[dict]) -> dict:
    from tracefix.comparison import digest

    task_ids = list(protocol["tasks"])
    planned = len(protocol["schedule"])
    planned_by_arm = Counter(r["arm"] for r in protocol["schedule"])
    repetitions = protocol["profile"]["repetitions"]
    rows = []
    for task in task_ids:
        row = {
            "task_id": task,
            "repository": protocol["tasks"][task].get("repository", task.split("__")[0]),
        }
        for arm in "ABC":
            subset = [r for r in records if r["task_id"] == task and r["arm"] == arm]
            row[arm] = {
                **aggregate(subset, requests),
                "finished": sum(bool(r.get("finished")) for r in subset),
            }
        rows.append(row)
    repositories = sorted({r["repository"] for r in rows})
    comparisons = {f"C-{a}": paired_statistics(rows, a, repetitions) for a in "AB"}
    return {
        "protocol_sha256": digest(protocol),
        "mode": protocol["mode"],
        "complete": len(records) == planned and all(r.get("finished") for r in records),
        "planned": planned,
        "started": len(records),
        "unstarted": planned - len(records),
        "arms": {
            a: {
                **aggregate([r for r in records if r["arm"] == a], requests),
                "planned": planned_by_arm[a],
            }
            for a in "ABC"
        },
        "comparisons": comparisons,
        "per_task": rows,
        "leave_one_repository_out": {
            repo: {
                f"C-{a}": paired_statistics(
                    [r for r in rows if r["repository"] != repo], a, repetitions
                )
                for a in "AB"
            }
            for repo in repositories
        },
        "by_repository": {
            repo: {
                a: aggregate(
                    [
                        r
                        for r in records
                        if r["arm"] == a
                        and r["task_id"] in {t["task_id"] for t in rows if t["repository"] == repo}
                    ],
                    requests,
                )
                for a in "ABC"
            }
            for repo in repositories
        },
        "infrastructure_sensitivity": {
            f"C-{a}": paired_statistics(
                [r for r in rows if not any(r[b]["infrastructure_failures"] for b in "ABC")],
                a,
                repetitions,
            )
            for a in "AB"
        },
        "unknown_requests": [r["id"] for r in requests if r["status"] != "completed"],
        "supplier_models": sorted(
            {r["response_model"] for r in requests if r.get("response_model")}
        ),
        "supplier_system_fingerprints": sorted(
            {
                r["supplier_system_fingerprint"]
                for r in requests
                if r.get("supplier_system_fingerprint")
            }
        ),
        "supplier_finish_reasons": dict(
            Counter(r.get("supplier_finish_reason", "unavailable") for r in requests)
        ),
        "interpretation": (
            "Project holdout; public training contamination unknown; cold start only."
        ),
    }
