"""Explicit, separately reported continuation using an unchanged frozen engine.

An unknown parent request is never retried or assigned a known cost. Its maximum
reservation remains a liability against the SAME authorization. All new unknown
requests still stop the child batch. The parent remains immutable.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from tracefix.checkpoint import ProcessLock
from tracefix.comparison import digest, file_sha, read_json, write_json
from tracefix.comparison_campaign import (
    check_protocol,
    execute_trial,
    finalize_trial,
    git,
    report,
    validate_record,
    validate_requests,
)
from tracefix.comparison_funding import prior_spend
from tracefix.comparison_profiles import AC_HOLDOUT_PROFILE, protocol_profile
from tracefix.runtime import load_environment_file


def check_driver(approval: dict) -> None:
    repository = Path(__file__).resolve().parents[1]
    ci = read_json(Path(approval["driver_ci_evidence"]))
    if (
        ci["head_sha"] != git(repository, "rev-parse", "HEAD")
        or ci["conclusion"] != "success"
        or len(ci["jobs"]) != 5
        or any(j["conclusion"] != "success" for j in ci["jobs"])
        or git(repository, "status", "--porcelain", "--", "scripts/continue_comparison.py")
    ):
        raise ValueError("exact clean continuation driver CI required")


def plan(parent: Path, approval: dict) -> tuple[dict, dict]:
    """Reconcile durable evidence; accept only explicitly named pending requests."""
    protocol = read_json(parent / "protocol.json")
    if digest(protocol) != read_json(parent / "protocol.sha256.json")["sha256"]:
        raise ValueError("parent protocol corrupted")
    if protocol_profile(protocol)["name"] != AC_HOLDOUT_PROFILE:
        raise ValueError("continuation requires the frozen A/C protocol")
    if (
        approval.get("kind") != "tracefix_bounded_continuation"
        or not approval.get("user_instruction")
        or approval.get("parent_campaign") != str(parent.resolve())
        or approval.get("parent_protocol_sha256") != file_sha(parent / "protocol.json")
        or approval.get("requests_sha256") != file_sha(parent / "requests.json")
    ):
        raise ValueError("explicit immutable parent approval required")
    requests = validate_requests(parent, protocol)
    audit_path = parent / "first-block-audit.json"
    if not read_json(audit_path).get("accepted"):
        raise ValueError("parent first-block audit required")
    if read_json(parent / "requests.json").get("halt_reason"):
        raise ValueError("parent has an additional counting anomaly")
    pending = {r["id"]: r["reserved_peak_cny"] for r in requests if r["status"] != "completed"}
    if not pending or pending != approval.get("unknown_reservations"):
        raise ValueError("unknown reservations do not match approval")
    if any(not math.isfinite(v) or v <= 0 for v in pending.values()):
        raise ValueError("invalid unknown liability")
    skip = {r["trial_id"] for r in requests if r["id"] in pending}
    remaining = []
    records = {}
    for row in protocol["schedule"]:
        path = parent / "trials" / row["id"] / "record.json"
        if not path.exists():
            if row["id"] in skip:
                raise ValueError("unknown request has no durable trial record")
            remaining.append(row)
            continue
        record = read_json(path)
        records[row["id"]] = file_sha(path)
        if row["id"] in skip:
            if record.get("finished") or record.get("status") != "unknown_request":
                raise ValueError("unknown trial state mismatch")
        elif record.get("finished"):
            validate_record(record, path.parent, row)
        else:
            raise ValueError("unapproved incomplete parent trial")
    liability = (
        prior_spend(protocol)
        + sum(r["peak_cost_cny"] for r in requests if r["status"] == "completed")
        + sum(pending.values())
    )
    if not math.isfinite(liability) or not 0 <= liability <= 300:
        raise ValueError("authorization liability invalid")
    funding = {
        "parent_campaign": str(parent.resolve()),
        "requests_sha256": file_sha(parent / "requests.json"),
        "parent_protocol_sha256": file_sha(parent / "protocol.json"),
        "prior_conservative_cny": liability,
        "authorization_cny": 300.0,
    }
    manifest = {
        "kind": "tracefix_bounded_continuation",
        "approval": approval,
        "skipped_unknown_trials": sorted(skip),
        "parent_records": records,
        "parent_audit_sha256": file_sha(audit_path),
        "funding": funding,
        "schedule": remaining,
        "driver_sha256": file_sha(Path(__file__)),
        "interpretation": "Separate batch; parent response/cost remains unknown. No replay.",
    }
    child = {**protocol, "schedule": remaining, "funding": funding, "continuation": manifest}
    return child, manifest


def prepare(parent: Path, root: Path, approval_path: Path) -> dict:
    parent, root = parent.resolve(), root.resolve()
    if parent == root or root.is_relative_to(parent):
        raise ValueError("child must be outside the immutable parent")
    with ProcessLock(parent), ProcessLock(root):
        check_protocol(parent)
        approval = read_json(approval_path)
        check_driver(approval)
        protocol, manifest = plan(parent, approval)
        claim_path = approval_path.with_suffix(".claim.json")
        claim = {"child": str(root), "approval_sha256": file_sha(approval_path)}
        if claim_path.exists() and read_json(claim_path) != claim:
            raise ValueError("continuation approval already claimed")
        if (root / "protocol.json").exists():
            if read_json(root / "protocol.json") != protocol:
                raise ValueError("existing continuation differs")
            return manifest
        write_json(claim_path, claim)
        write_json(root / "protocol.json", protocol)
        write_json(root / "protocol.sha256.json", {"sha256": digest(protocol)})
        write_json(root / "continuation.json", manifest)
        write_json(root / "approval-identity.json", {"path": str(approval_path.resolve()), **claim})
        return manifest


def run(root: Path, env_file: Path | None, *, max_trials: int = 60) -> list[dict]:
    with ProcessLock(root):
        protocol = read_json(root / "protocol.json")
        manifest = protocol["continuation"]
        identity = read_json(root / "approval-identity.json")
        approval_path = Path(identity["path"])
        check_driver(read_json(approval_path))
        if (
            identity["child"] != str(root.resolve())
            or file_sha(approval_path) != identity["approval_sha256"]
            or read_json(approval_path.with_suffix(".claim.json"))
            != {k: identity[k] for k in ("child", "approval_sha256")}
        ):
            raise ValueError("continuation claim changed")
        parent = Path(manifest["funding"]["parent_campaign"])
        with ProcessLock(parent):
            check_protocol(parent)
            expected, _ = plan(parent, read_json(approval_path))
        if (
            protocol != expected
            or digest(protocol) != read_json(root / "protocol.sha256.json")["sha256"]
            or read_json(root / "continuation.json") != manifest
        ):
            raise ValueError("continuation identity changed")
        requests = validate_requests(root, protocol)
        if any(r["status"] != "completed" for r in requests):
            raise ValueError("child unknown request; no automatic replay")
        if (root / "halt.json").exists() or (
            (root / "requests.json").exists()
            and read_json(root / "requests.json").get("halt_reason")
        ):
            raise ValueError("child halted; no automatic replay")
        if protocol["mode"] == "live":
            load_environment_file(env_file)
        results = []
        for row in protocol["schedule"]:
            directory = root / "trials" / row["id"]
            path = directory / "record.json"
            if path.exists():
                previous = read_json(path)
                if not previous.get("finished"):
                    if previous.get("phase") != "agent_completed":
                        raise ValueError("incomplete child trial; no automatic replay")
                    previous = finalize_trial(
                        directory, previous, protocol["tasks"][row["task_id"]]
                    )
                validate_record(previous, directory, row)
                results.append(previous)
                continue
            if len(results) >= max_trials:
                break
            block_path = root / "blocks" / f"{row['block']:02d}.json"
            members = [r["id"] for r in protocol["schedule"] if r["block"] == row["block"]]
            if not block_path.exists():
                spent = sum(r["peak_cost_cny"] for r in validate_requests(root, protocol))
                remaining = 300 - prior_spend(protocol) - spent
                reservation = len(members) * protocol_profile(protocol)["trial_limit_cny"]
                if round(remaining * 1_000_000) < round(reservation * 1_000_000):
                    write_json(
                        root / "budget-stop.json", {"sent": False, "remaining_cny": remaining}
                    )
                    break
                write_json(block_path, {"protocol_sha256": digest(protocol), "trials": members})
            if read_json(block_path) != {"protocol_sha256": digest(protocol), "trials": members}:
                raise ValueError("child block changed")
            result = execute_trial(root, protocol, row, env_file)
            results.append(result)
            print(
                {
                    "id": row["id"],
                    "arm": row["arm"],
                    "status": result["status"],
                    "passed": result["passed"],
                },
                flush=True,
            )
            if result.get("infrastructure_failure"):
                write_json(
                    root / "halt.json", {"trial": row["id"], "reason": "infrastructure failure"}
                )
                raise ValueError("child infrastructure failure; halted")
            if read_json(root / "requests.json").get("halt_reason"):
                raise ValueError("child counting anomaly; halted")
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "report"))
    parser.add_argument("--parent", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--max-trials", type=int, default=60)
    args = parser.parse_args()
    if args.action == "prepare":
        if not args.parent or not args.approval:
            parser.error("prepare requires --parent and --approval")
        print(prepare(args.parent, args.campaign_dir, args.approval))
    elif args.action == "run":
        run(args.campaign_dir, args.env_file, max_trials=args.max_trials)
    else:
        print(report(args.campaign_dir))


if __name__ == "__main__":
    main()
