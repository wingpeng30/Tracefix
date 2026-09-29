"""Combine separate offline replays without changing historical scores."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def stable_qualification(executions: list[dict]) -> bool:
    if len(executions) != 2 or {item["repeat"] for item in executions} != {1, 2}:
        return False
    results = [item["result"] for item in executions]
    if not all(result.get("eligible_for_llm_prescreen") is True for result in results):
        return False
    for field in ("initial_evidence", "gold_evidence"):
        if results[0][field]["status"] != results[1][field]["status"]:
            return False
        if results[0][field]["executed_node_ids"] != results[1][field]["executed_node_ids"]:
            return False
    return all(not result.get("dependency_drift_detected") for result in results)


def classify_replay(executions: list[dict]) -> str:
    if len(executions) != 2 or {item["repeat"] for item in executions} != {1, 2}:
        return "unresolved"
    results = [item["result"] for item in executions]
    if all(result.get("eligible") is True for result in results):
        return "verified_repair"
    evidence = [result.get("evidence") or {} for result in results]
    if (
        all(result.get("eligible") is False for result in results)
        and len({result.get("reason") for result in results}) == 1
        and all(
            item.get("status") in {"assertion_failed", "assertion_failed_with_phase_errors"}
            and item.get("audit_available")
            and item.get("collection_audit_available")
            and item.get("source_import_audit_valid")
            for item in evidence
        )
        and evidence[0].get("executed_node_ids") == evidence[1].get("executed_node_ids")
    ):
        return "verified_repair_failure"
    return "environment_or_evidence_unresolved"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    snapshot = load(root / "source-manifest.json")
    for name, expected in snapshot["frozen_files"].items():
        path = Path(name)
        if not path.is_absolute():
            path = Path.cwd() / path
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"frozen input changed: {name}")
    inventory = load(root / "inventory/report.json")
    qualifications = (
        load(root / "qualification-requests/report.json")["executions"]
        + load(root / "qualification-other-elevated/report.json")["executions"]
    )
    patches = (
        load(root / "patches-requests/report.json")["executions"]
        + load(root / "patches-other-elevated/report.json")["executions"]
    )
    by_task: dict[str, list[dict]] = collections.defaultdict(list)
    by_sequence: dict[int, list[dict]] = collections.defaultdict(list)
    for item in qualifications:
        by_task[item["task_id"]].append(item)
    for item in patches:
        by_sequence[item["sequence"]].append(item)
    rows = []
    for item in inventory["rows"]:
        sequence, task = item["sequence"], item["task_id"]
        replay = by_sequence[sequence]
        if item["original_category"] != "evidence_issue":
            conclusion = "historical_evidence_only"
        elif not stable_qualification(by_task[task]):
            conclusion = "environment_or_evidence_unresolved"
        elif item["derived_outcome"] == "no_product_change":
            conclusion = "no_product_change"
        else:
            conclusion = classify_replay(replay)
        rows.append(
            {
                "sequence": sequence,
                "task_id": task,
                "arm": item["arm"],
                "original_category": item["original_category"],
                "original_reason": item["original_reason"],
                "product_patch_sha256": item["historical_patch"]["product_sha256"],
                "product_patch_bytes": item["historical_patch"]["product_bytes"],
                "runner_artifacts_removed": len(item["historical_patch"]["removed_runner_files"]),
                "qualified_twice": stable_qualification(by_task[task]) if task in by_task else None,
                "replay_count": len(replay),
                "replay_statuses": [
                    (entry["result"].get("evidence") or {}).get("status") for entry in replay
                ],
                "derived_conclusion": conclusion,
            }
        )
    report = {
        "kind": "frozen_48_evidence_rescue_summary",
        "head": snapshot["head"],
        "source_manifest_sha256": hashlib.sha256(
            (root / "source-manifest.json").read_bytes()
        ).hexdigest(),
        "original_category_counts": dict(
            collections.Counter(row["original_category"] for row in rows)
        ),
        "derived_issue_counts": dict(
            collections.Counter(
                row["derived_conclusion"]
                for row in rows
                if row["original_category"] == "evidence_issue"
            )
        ),
        "rows": rows,
        "interpretation": (
            "Derived replays assess saved patches in the documented recovery environment; "
            "historical scoring is unchanged."
        ),
    }
    (root / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 冻结 48 项证据复验",
        "",
        "原评分保留；以下结论仅来自旧补丁派生复验。",
        "",
        "| 序号 | 任务 | 原分类 | 产品补丁字节 | 复验次数 | 派生结论 |",
        "| ---: | --- | --- | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['sequence']} | {row['task_id']} | {row['original_category']} | "
            f"{row['product_patch_bytes']} | {row['replay_count']} | {row['derived_conclusion']} |"
        )
    (root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(root / "summary.md")


if __name__ == "__main__":
    main()
