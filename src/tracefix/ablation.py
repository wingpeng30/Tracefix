"""Read-only, task-weighted analysis of the four-arm development experiment."""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from statistics import mean


def budget_preflight(
    *,
    cap: str,
    spent: str,
    reserved: str,
    input_price: str,
    output_price: str,
    trials: int = 120,
    input_tokens: int = 350_000,
    output_tokens: int = 20_000,
) -> dict:
    values = [Decimal(x) for x in (cap, spent, reserved, input_price, output_price)]
    if any(not x.is_finite() or x < 0 for x in values):
        raise ValueError("budget amounts must be finite and nonnegative")
    ceiling, used, pending, price_in, price_out = values
    per_trial = (input_tokens * price_in + output_tokens * price_out) / Decimal(1_000_000)
    remaining = ceiling - used - pending
    return {
        "currency": "CNY",
        "amount_kind": "conservative_calculated_upper_bound",
        "cap": str(ceiling),
        "spent": str(used),
        "reserved": str(pending),
        "remaining": str(remaining),
        "per_trial": str(per_trial),
        "development_trials": trials,
        "development_upper_bound": str(trials * per_trial),
        "holdout_trials": 120,
        "holdout_upper_bound": str(120 * per_trial),
        "both_stages_upper_bound": str((trials + 120) * per_trial),
        "development_fully_funded": remaining >= trials * per_trial,
        "both_stages_fully_funded": remaining >= (trials + 120) * per_trial,
        "paid_execution_authorized_by_this_report": False,
    }


def summarize_ablation(experiment_dir: Path) -> dict:
    from tracefix.p2_protocol import _audit_p2_evidence, _read_trial

    audit = _audit_p2_evidence(experiment_dir)
    protocol = audit["protocol"]
    if protocol.kind != "p2_four_arm_ablation_protocol":
        raise ValueError("not a four-arm ablation protocol")
    rows = audit["rows"]
    arms = list(dict.fromkeys(plan.arm.value for plan in protocol.schedule))
    metrics = (
        "input_tokens",
        "output_tokens",
        "agent_duration_seconds",
        "verification_duration_seconds",
        "calculated_cost_amount",
    )
    for row in rows:
        record = _read_trial(experiment_dir / "trials" / f"{row['sequence']:03d}.json")
        row["completed"] = bool(record and record.status == "verification_complete")
        row["independent_passed"] = bool(row["independent_passed"] and row["completed"])
        row["verification_duration_seconds"] = (
            record.verification_duration_seconds if record else None
        )
        row["calculated_cost_amount"] = record.calculated_cost_amount if record else None
        row["cost_currency"] = record.cost_currency if record else None
        if not row["evidence_valid"] or not row["completed"]:
            for metric in metrics:
                row[metric] = None
    task_results = []
    for task_id in protocol.qualified_task_ids:
        groups = {}
        for arm in arms:
            selected = [r for r in rows if r["task_id"] == task_id and r["arm"] == arm]
            groups[arm] = {
                "planned": len(selected),
                "completed": sum(r["completed"] for r in selected),
                "planned_success_lower_bound": sum(r["independent_passed"] for r in selected) / 3,
                "success_rate": sum(r["independent_passed"] for r in selected) / 3
                if len(selected) == 3
                and all(r["completed"] and r["evidence_valid"] for r in selected)
                else None,
                "valid_evidence": sum(r["evidence_valid"] for r in selected),
                **{
                    metric: mean(r[metric] for r in selected)
                    if len(selected) == 3 and all(r[metric] is not None for r in selected)
                    else None
                    for metric in metrics
                },
            }
        task_results.append({"task_id": task_id, "arms": groups})
    comparisons = {}
    for label, task_ids in (
        ("ordinary", protocol.primary_task_ids),
        ("reviewed_collection_failure", protocol.collection_failure_task_ids),
    ):
        comparisons[label] = {}
        subset = [t for t in task_results if t["task_id"] in task_ids]
        for arm in arms:
            if arm == "no_compaction":
                continue
            differences = {}
            for metric in ("success_rate", *metrics):
                pairs = [
                    (t["arms"][arm][metric], t["arms"]["no_compaction"][metric]) for t in subset
                ]
                differences[metric] = (
                    mean(a - b for a, b in pairs)
                    if pairs and all(a is not None and b is not None for a, b in pairs)
                    else None
                )
            both_success = sum(
                all(
                    any(
                        r["task_id"] == task_id
                        and r["repetition"] == rep
                        and r["arm"] == a
                        and r["independent_passed"]
                        for r in rows
                    )
                    for a in ("no_compaction", arm)
                )
                for task_id in task_ids
                for rep in range(1, 4)
            )
            comparisons[label][arm] = {
                "task_count": len(subset),
                "task_equal_weight_delta": differences,
                "both_success_pairs": both_success,
            }
    return {
        "schema_version": 1,
        "kind": "p2_four_arm_ablation_summary",
        "protocol_sha256": audit["protocol_sha256"],
        "planned_count": len(rows),
        "completed_count": sum(r["completed"] for r in rows),
        "success_count": sum(r["independent_passed"] for r in rows),
        "repair_failure_count": sum(
            r["completed"]
            and r["evidence_valid"]
            and not r["independent_passed"]
            and not r["infrastructure_error"]
            for r in rows
        ),
        "unexecuted_count": sum(r["termination_category"] == "unexecuted" for r in rows),
        "evidence_issue_count": sum(not r["evidence_valid"] for r in rows),
        "infrastructure_count": sum(r["infrastructure_error"] for r in rows),
        "verification_error_count": sum(bool(r.get("verification_indeterminate")) for r in rows),
        "valid_evidence_count": sum(r["evidence_valid"] for r in rows),
        "termination_categories": dict(Counter(r["termination_category"] for r in rows)),
        "task_results": task_results,
        "comparisons": comparisons,
        "trials": rows,
        "evidence_audit_version": audit["schema_version"],
        "interpretation": (
            "Simulation verifies engineering only; no repair-effect inference. "
            "Unusable verification is excluded from repair failures and effect estimates; "
            "its cause may be the patch or the environment. Original verdicts are unchanged."
        ),
    }


def write_ablation_summary(experiment_dir: Path) -> Path:
    summary = summarize_ablation(experiment_dir)
    path = experiment_dir / "p2-summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (experiment_dir / "p2-summary.md").write_text(
        "# 四组消融汇总\n\n"
        f"计划 {summary['planned_count']} 项，完成 {summary['completed_count']} 项，"
        f"证据有效 {summary['valid_evidence_count']} 项，"
        f"独立验收成功 {summary['success_count']} 项。\n"
        "逐题先汇总三次重复，再按任务等权比较。普通资格和收集失败资格分列。\n"
        "模拟演练仅证明工程流程；未知用量不填零，全部计划位置保留。\n",
        encoding="utf-8",
    )
    return path
