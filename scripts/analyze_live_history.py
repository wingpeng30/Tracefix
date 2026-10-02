"""Audit real histories offline; reports contain metrics, never raw model messages."""

from __future__ import annotations

import argparse
import csv
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

from history_analysis import analyze_run, file_sha, load_events

from tracefix.regression_replay import forbid_live_access


def analyze(campaign: Path, output: Path, *, micro_python: Path | None = None) -> dict:
    campaign, output = campaign.resolve(), output.resolve()
    if output.exists():
        raise ValueError("analysis output already exists")
    if output.is_relative_to(campaign):
        raise ValueError("output must be outside the original campaign")
    inputs = [
        p
        for p in campaign.rglob("*")
        if p.is_file() and ".git" not in p.parts and "workspace" not in p.parts
    ]
    original = {str(p): file_sha(p) for p in inputs}
    output.mkdir(parents=True)
    ledger = json.loads((campaign / "requests.json").read_text(encoding="utf-8"))
    runs = json.loads((campaign / "runs.json").read_text(encoding="utf-8"))
    with forbid_live_access():
        findings = []
        offset = 0
        for record in runs["runs"]:
            run = Path(record["result_path"]).parent
            events, _ = load_events(run / "trajectory.jsonl")
            count = sum(e["event_type"] == "model_responded" for e in events)
            findings.append(
                analyze_run(record, campaign, ledger["requests"][offset : offset + count])
            )
            offset += count
    matched = [sha for run in findings for sha in run["matched_request_hashes"]]
    micro_results = []
    if micro_python and all(not r["errors"] and r["stop_reproduced"] for r in findings):
        from history_micro import run_micro

        micro_results = [
            run_micro(output / ("micro-" + name), micro_python, name)
            for name in ("line_pages", "context")
        ]
    result = {
        "schema_version": 1,
        "kind": "offline_diagnosis_not_repair_success_evaluation",
        "byte_metric_scope": "canonical LiteLLM kwargs, not observed HTTP bytes or provider tokens",
        "implementation_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "scripts_sha256": {
            p.name: file_sha(p)
            for p in (
                Path(__file__),
                Path(__file__).with_name("history_analysis.py"),
                Path(__file__).with_name("history_micro.py"),
            )
        },
        "interpreter": {"executable": sys.executable, "version": sys.version},
        "dependency_versions": dependency_versions(),
        "input_sha256": original,
        "ledger_requests": len(ledger["requests"]),
        "matched_requests": len(matched),
        "ledger_pending": sum(r.get("status") != "completed" for r in ledger["requests"]),
        "ledger_identity_complete": len(matched) == len(ledger["requests"])
        and offset == len(ledger["requests"]),
        "runs": findings,
        "micro_results": micro_results,
        "supplier_request_attempts": 0,
        "inputs_unchanged": all(file_sha(Path(p)) == sha for p, sha in original.items()),
        "candidate_policy": "reject evidence loss; no counterfactual provider usage",
    }
    (output / "analysis.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    fields = [
        "stage",
        "step",
        "transmission",
        "baseline_reproduced",
        "hash_verified",
        "production_estimate",
        "wire_bytes",
        "byte_reservation",
        "provider_input",
        "cumulative_input_after",
    ]
    with (output / "requests.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for run in findings:
            for row in run["requests"]:
                writer.writerow(
                    {
                        **{
                            key: row.get(key)
                            for key in fields
                            if key not in ("stage", "provider_input")
                        },
                        "stage": run["stage"],
                        "provider_input": (row.get("observed_usage") or {}).get("input_tokens"),
                    }
                )
    report = [
        "# Offline history investigation",
        "",
        "No provider requests. Changed views have no observed usage; no success-rate claim.",
        "",
        "| Run | Paired requests | Baseline | Stop reproduced | Read classifications |",
        "| --- | --- | --- | --- | --- |",
    ]
    from collections import Counter

    for run in findings:
        report.append(
            f"| {run['stage']} | {len(run['matched_request_hashes'])} | "
            f"{not run['errors']} | {run['stop_reproduced']} | "
            f"{dict(Counter(r['classification'] for r in run['reads']))} |"
        )
    report.extend(
        [
            "",
            "Candidate comparisons are in analysis.json. Evidence-loss failures exclude "
            "projections from adoption. Full original messages are not copied into this report.",
        ]
    )
    report.extend(
        [
            "",
            "| Projection | Baseline bytes | Projected bytes | Evidence-loss views | "
            "Smaller intact views |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for name in ("line_pages", "threshold_6000", "remaining_budget_context"):
        pairs = [
            (row, candidate)
            for run in findings
            for row in run["requests"]
            for candidate in row["candidates"]
            if candidate["name"] == name
        ]
        report.append(
            f"| {name} | {sum(r['wire_bytes'] for r, c in pairs)} | "
            f"{sum(c['wire_bytes'] for r, c in pairs)} | "
            f"{sum(bool(c['evidence_failures']) for r, c in pairs)} | "
            f"{sum(c['eligible'] for r, c in pairs)} |"
        )
    report.extend(
        [
            "",
            "Byte reservations are conservative request guards, not provider token counts. "
            "Candidate fit uses the recorded original usage prefix, not counterfactual usage. "
            "Evidence checks preserve all baseline-visible source lines conservatively; "
            "they do not prove that every removed line is necessary to a model decision.",
        ]
    )
    (output / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return result


def dependency_versions():
    result = {}
    for name in ("pydantic", "pytest", "litellm", "tokenizers", "transformers"):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test-python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    result = analyze(args.campaign, args.output, micro_python=args.test_python.resolve())
    print(
        json.dumps(
            {
                "output": str(args.output),
                "matched_requests": result["matched_requests"],
                "inputs_unchanged": result["inputs_unchanged"],
            }
        )
    )
    return (
        0
        if result["ledger_identity_complete"]
        and result["inputs_unchanged"]
        and all(not r["errors"] and r["stop_reproduced"] for r in result["runs"])
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
