"""Check copied evidence and repeat consistency for offline container replays."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(path: Path) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    results = report["results"]
    if [(item["stage"], item["repeat"]) for item in results] != [
        ("qualification", 1), ("qualification", 2), ("patch", 1), ("patch", 2)
    ]:
        raise ValueError(f"incomplete or reordered repeat set: {path}")
    if not all(item["qualified"] for item in results):
        raise ValueError(f"one or more replays unqualified: {path}")
    node_sets = []
    evidence_count = 0
    for item in results:
        for relative, expected_hash in item["evidence_sha256"].items():
            evidence = (path.parent / relative).resolve(strict=True)
            if (
                not evidence.is_relative_to(path.parent.resolve())
                or digest(evidence) != expected_hash
            ):
                raise ValueError(f"evidence changed or escaped report: {relative}")
            evidence_count += 1
        payload = item["result"]
        evidences = (
            (payload["initial_evidence"], payload["gold_evidence"])
            if item["stage"] == "qualification" else (payload["evidence"],)
        )
        for evidence in evidences:
            if not (evidence["audit_available"] and evidence["junit_available"]
                    and evidence["collection_audit_available"]
                    and evidence["source_import_audit_valid"]):
                raise ValueError(f"incomplete strict evidence: {path}")
            if evidence["skipped_count"] or evidence["xfailed_count"] or evidence["xpassed_count"]:
                raise ValueError(f"unexpected nonbusiness outcome: {path}")
            node_sets.append(frozenset(evidence["executed_node_ids"]))
        if payload["dependency_drift_detected"]:
            raise ValueError(f"dependency drift: {path}")
    if not node_sets[0] or len(set(node_sets)) != 1:
        raise ValueError(f"base/gold/patch node sets disagree: {path}")
    return {
        "task_id": report["task_id"],
        "sequence": report["sequence"],
        "report_sha256": digest(path),
        "evidence_files_verified": evidence_count,
        "executed_node_ids": sorted(node_sets[0]),
        "repeat_consistent": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("audit output must be new")
    rows = [audit(path) for path in args.reports]
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)
