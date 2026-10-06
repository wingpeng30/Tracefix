"""Recheck raw pass/fail/source evidence before accepting a holdout qualification index."""

from __future__ import annotations

import argparse
from pathlib import Path

from tracefix.comparison import file_sha, read_json, write_json
from tracefix.comparison_campaign import source_identity
from tracefix.comparison_holdout import HOLDOUT_TASK_IDS
from tracefix.provenance import inspect_test_environment


def audit(catalog: Path, roots: list[Path]) -> dict:
    specifications = read_json(catalog)
    if [s["task_id"] for s in specifications] != list(HOLDOUT_TASK_IDS):
        raise ValueError("not the sealed twenty-task catalog")
    rows = []
    for spec in specifications:
        matched = None
        for root in roots:
            path = root / "qualification" / spec["task_id"] / "comparison-qualification.json"
            if not path.is_file():
                continue
            qualified = read_json(path)
            if all(
                qualified.get(k) == spec.get(k)
                for k in ("task_id", "source", "python", "recipe", "regressions")
            ):
                matched = (path, qualified)
                break
        if matched is None:
            raise ValueError("no exact matching qualification: " + spec["task_id"])
        path, qualified = matched
        behavior = read_json(path.parent / "behavior.json")
        if (
            behavior != qualified["qualification"]
            or not behavior["eligible_for_llm_prescreen"]
            or behavior["qualification_type"] != "assertion_failure"
            or not behavior["initial_hidden_failed"]
            or not behavior["gold_hidden_passed"]
            or behavior["initial_returncode"] != 1
            or behavior["gold_returncode"] != 0
            or behavior["dependency_drift_detected"]
            or behavior["initial_evidence"]["status"] != "assertion_failed"
            or behavior["gold_evidence"]["status"] != "passed"
        ):
            raise ValueError("raw base/reference qualification did not pass")
        for stage in ("initial_evidence", "gold_evidence"):
            evidence = behavior[stage]
            if not all(
                evidence.get(k)
                for k in (
                    "junit_available",
                    "audit_available",
                    "collection_audit_available",
                    "source_import_audit_valid",
                )
            ):
                raise ValueError("raw source/test audit incomplete")
        artifacts = {
            str(path): file_sha(path),
            str(path.parent / "behavior.json"): file_sha(path.parent / "behavior.json"),
        }
        for stage in ("initial", "gold"):
            result_path = path.parent / f"regression-{stage}/result.json"
            result = read_json(result_path)
            output = result.get("output") or {}
            counts = output.get("test_counts") or {}
            if (
                not result["success"]
                or output.get("test_status") != "passed"
                or output.get("returncode") != 0
                or not counts.get("tests")
                or any(counts.get(k, 0) for k in ("failures", "errors", "skipped"))
                or sorted(counts.get("node_ids", [])) != qualified["regression_nodes"]
            ):
                raise ValueError("raw regression qualification did not pass")
            artifacts[str(result_path)] = file_sha(result_path)
        if source_identity(Path(spec["source"])) != qualified["source_identity"]:
            raise ValueError("qualified source changed")
        current = inspect_test_environment(
            Path(spec["python"]),
            pythonpath_entries=tuple(Path(p) for p in spec.get("pythonpath", [])),
        )
        if current.fingerprint_sha256 != qualified["environment_identity"]:
            raise ValueError("qualified dependency identity changed")
        rows.append(
            {
                "task_id": spec["task_id"],
                "accepted": True,
                "qualification_path": str(path),
                "raw_artifact_sha256": artifacts,
            }
        )
    return {
        "supplier_calls": 0,
        "all_passed": len(rows) == 20,
        "catalog_sha256": file_sha(catalog),
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--qualification-root", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = audit(args.catalog.resolve(), [p.resolve() for p in args.qualification_root])
    write_json(args.output.resolve(), evidence)
    print("Raw qualification rechecked:", len(evidence["rows"]), "supplier calls: 0")


if __name__ == "__main__":
    main()
