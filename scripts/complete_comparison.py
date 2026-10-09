"""Run a separately frozen suffix, retaining unknown fees without replay.

Each trial has a sealed ledger. A subsequent trial includes all preceding known
costs and unknown maximum reservations in its funding chain. Product code stays
unchanged. Only provider unknowns are terminal failures; other anomalies halt.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import shutil
import sys
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener
from urllib.request import build_opener as opener_factory

from tracefix.checkpoint import ProcessLock
from tracefix.comparison import digest, file_sha, read_json, write_json
from tracefix.comparison_campaign import (
    execute_trial,
    finalize_trial,
    git,
    report,
    schedule,
    source_identity,
    validate_record,
    validate_requests,
)
from tracefix.comparison_funding import prior_spend
from tracefix.comparison_profiles import (
    AC_HOLDOUT_PROFILE,
    OfficialCounter,
    protocol_profile,
)
from tracefix.comparison_selection import validate_selection
from tracefix.comparison_transport import transport_identity
from tracefix.provenance import inspect_test_environment
from tracefix.runtime import load_environment_file


def load_validator(approval: dict):
    path = Path(approval["parent_driver"])
    if file_sha(path) != approval["parent_driver_sha256"]:
        raise ValueError("parent driver identity changed")
    spec = importlib.util.spec_from_file_location("frozen_continuation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_driver(approval: dict) -> None:
    repository = Path(__file__).resolve().parents[1]
    ci_path = approval.get("driver_ci_evidence")
    if ci_path:
        ci = read_json(Path(ci_path))
        if (
            ci["head_sha"] == git(repository, "rev-parse", "HEAD")
            and ci["conclusion"] == "success"
            and len(ci["jobs"]) == 5
            and all(j["conclusion"] == "success" for j in ci["jobs"])
            and not git(repository, "status", "--porcelain", "--", "scripts/complete_comparison.py")
        ):
            return
    # Explicitly scoped user direction can waive the wait for exact-commit CI;
    # require a hash-bound local acceptance receipt and retain this deviation.
    evidence = read_json(Path(approval["direct_start_evidence"]))
    if (
        approval.get("direct_start_override")
        != "用户要求尽可能直接付费运行；接受新控制器尚无精确提交 CI"
        or evidence.get("accepted") is not True
        or evidence.get("driver_sha256") != file_sha(Path(__file__))
        or evidence.get("tests_sha256")
        != file_sha(repository / "tests/test_comparison_completion.py")
        or evidence.get("local_tests_passed") != 9
        or evidence.get("previous_driver_ci_conclusion") != "success"
    ):
        raise ValueError("exact CI or explicit hash-bound direct-start acceptance required")


def funding(root: Path, protocol: dict) -> dict:
    requests = validate_requests(root, protocol)
    ledger = read_json(root / "requests.json") if (root / "requests.json").exists() else {}
    if (root / "halt.json").exists() or ledger.get("halt_reason"):
        raise ValueError("non-provider anomaly blocks continuation")
    if any(r.get("official_count_delta", 0) != 0 for r in requests if r["status"] == "completed"):
        raise ValueError("tokenizer counting anomaly")
    costs = [
        r["peak_cost_cny"] if r["status"] == "completed" else r["reserved_peak_cny"]
        for r in requests
    ]
    if any(not math.isfinite(c) or c < 0 for c in costs):
        raise ValueError("invalid liability")
    liability = prior_spend(protocol) + sum(costs)
    if not math.isfinite(liability) or not 0 <= liability <= 300:
        raise ValueError("authorization exhausted or invalid")
    return {
        "parent_campaign": str(root.resolve()),
        "requests_sha256": file_sha(root / "requests.json"),
        "parent_protocol_sha256": file_sha(root / "protocol.json"),
        "prior_conservative_cny": liability,
        "authorization_cny": 300.0,
    }


def wallet(root: Path, block: int) -> bool:
    request = Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"]},
    )
    with build_opener(ProxyHandler({})).open(request, timeout=60) as response:
        balance = json.loads(response.read())
    write_json(root / "wallet" / f"{block:02d}.json", balance)
    cny = next(b for b in balance["balance_infos"] if b["currency"] == "CNY")
    return bool(balance["is_available"]) and float(cny["total_balance"]) > 0


def verify_restarted_service(previous: dict, current: dict) -> None:
    mutable = {"pid", "started_at", "startup_nonce", "identity_url"}
    if any(previous.get(k) != current.get(k) for k in previous if k not in mutable):
        raise ValueError("restarted service code or environment changed")
    if current.get("port") != previous.get("port") or current.get("health_url") != previous.get(
        "health_url"
    ):
        raise ValueError("restarted service endpoint changed")
    opener = opener_factory(ProxyHandler({}))
    with opener.open(current["identity_url"], timeout=5) as response:
        if json.load(response) != current:
            raise ValueError("service identity endpoint mismatch")
    with opener.open(current["health_url"], timeout=5) as response:
        if json.load(response).get("url") != current["health_url"]:
            raise ValueError("service health check failed")


def verify_frozen_environment(anchor: Path, new_service: dict) -> dict:
    """Verify the original freeze, accepting only an attested same-build restart."""
    original = read_json(anchor / "protocol.json")
    if digest(original) != read_json(anchor / "protocol.sha256.json")["sha256"]:
        raise ValueError("original protocol changed")
    selected = protocol_profile(original)
    if selected["name"] != AC_HOLDOUT_PROFILE or original["schedule"] != schedule(
        selected, list(original["tasks"])
    ):
        raise ValueError("original A/C protocol changed")
    if original["provider_transport"] != transport_identity():
        raise ValueError("provider transport identity changed")
    if prior_spend(original) != 0 or original["funding"].get("campaign") != str(anchor.resolve()):
        raise ValueError("original authorization changed")
    validate_selection(read_json(anchor / "task-selection.json"), list(original["tasks"]))
    OfficialCounter(original["counter_runtime"]).invoke()
    calibration = read_json(anchor / "tokenizer-calibration.json")
    if not calibration["accepted"] or calibration["runtime"] != original["counter_runtime"]:
        raise ValueError("tokenizer calibration changed")
    from tracefix import TraceFixRunner

    engine = Path(sys.modules["tracefix"].__file__).resolve().parents[2]
    if (
        git(engine, "rev-parse", "HEAD") != original["implementation_commit"]
        or TraceFixRunner._implementation_sha256() != original["implementation_sha256"]
        or original["product_base"] != "e380329d2a007a83dd317944bafd426d4b829c82"
    ):
        raise ValueError("frozen engine identity changed")
    for name, expected in original["artifacts"].items():
        path = (anchor / name).resolve()
        if not path.is_relative_to(anchor.resolve()) or file_sha(path) != expected:
            raise ValueError("frozen prompt/material changed")
    ci = read_json(anchor / "ci-evidence.json")
    if (
        ci["head_sha"] != original["implementation_commit"]
        or ci["conclusion"] != "success"
        or len(ci["jobs"]) != 5
        or any(job["conclusion"] != "success" for job in ci["jobs"])
    ):
        raise ValueError("original engine CI gate changed")
    previous = None
    for spec in original["tasks"].values():
        if source_identity(Path(spec["source"])) != spec["source_identity"]:
            raise ValueError("frozen source changed")
        environment = inspect_test_environment(
            Path(spec["python"]),
            pythonpath_entries=tuple(Path(p) for p in spec.get("pythonpath", [])),
        )
        if environment.fingerprint_sha256 != spec["environment_identity"]:
            raise ValueError("frozen test environment changed")
        identity = spec.get("service_identity")
        if identity:
            if previous is not None and identity != previous:
                raise ValueError("frozen service identities diverge")
            previous = identity
    if not previous:
        raise ValueError("original controlled service identity missing")
    verify_restarted_service(previous, new_service)
    return {"previous": previous, "current": new_service}


def plan(parent: Path, approval: dict) -> dict:
    protocol = read_json(parent / "protocol.json")
    if (
        approval.get("kind") != "tracefix_unknown_terminal_suffix"
        or approval.get("future_provider_unknown") != "fail_hold_reserve_continue"
        or not approval.get("user_instruction")
        or approval.get("parent_campaign") != str(parent.resolve())
        or approval.get("parent_protocol_sha256") != file_sha(parent / "protocol.json")
        or approval.get("requests_sha256") != file_sha(parent / "requests.json")
        or digest(protocol) != read_json(parent / "protocol.sha256.json")["sha256"]
    ):
        raise ValueError("explicit immutable suffix approval required")
    validator = load_validator(approval)
    previous_approval = protocol["continuation"]["approval"]
    validator.check_driver(previous_approval)
    anchor = Path(previous_approval["parent_campaign"])
    service_path = Path(approval["service_identity_file"])
    if file_sha(service_path) != approval["service_identity_sha256"]:
        raise ValueError("restarted service identity file changed")
    current_service = read_json(service_path)
    rebound = verify_frozen_environment(anchor, current_service)
    expected, _ = validator.plan(anchor, previous_approval)
    if expected != protocol:
        raise ValueError("parent continuation identity changed")
    identity = read_json(parent / "approval-identity.json")
    if (
        file_sha(Path(identity["path"])) != identity["approval_sha256"]
        or read_json(anchor.with_name(anchor.name + "-continuation.claim.json"))
        != {k: identity[k] for k in ("child", "approval_sha256")}
        or identity["child"] != str(parent.resolve())
    ):
        raise ValueError("parent claim changed")
    requests = validate_requests(parent, protocol)
    pending = {r["id"]: r["reserved_peak_cny"] for r in requests if r["status"] != "completed"}
    if not pending or pending != approval["unknown_reservations"]:
        raise ValueError("parent unknown reservations changed")
    skip = {r["trial_id"] for r in requests if r["id"] in pending}
    remaining, records = [], {}
    for row in protocol["schedule"]:
        path = parent / "trials" / row["id"] / "record.json"
        if not path.exists():
            if row["id"] in skip:
                raise ValueError("unknown trial has no evidence")
            remaining.append(row)
            continue
        record = read_json(path)
        records[row["id"]] = file_sha(path)
        if row["id"] in skip:
            if record.get("finished") or record.get("status") != "unknown_request":
                raise ValueError("unknown trial state changed")
        else:
            validate_record(record, path.parent, row)
            if not record.get("finished"):
                raise ValueError("unapproved incomplete parent trial")
    rebound_tasks = {
        task_id: {**spec, "service_identity": current_service}
        for task_id, spec in protocol["tasks"].items()
    }
    return {
        **protocol,
        "tasks": rebound_tasks,
        "schedule": remaining,
        "funding": funding(parent, protocol),
        "completion": {
            "approval": approval,
            "parent_records": records,
            "skipped_unknown_trials": sorted(skip),
            "driver_sha256": file_sha(Path(__file__)),
            "interpretation": "Separate suffix; provider unknowns fail and are never replayed.",
            "service_rebind": {
                "reason": (
                    "The previous owned process exited. Rechecked all frozen source and Python "
                    "environment fingerprints; restarted the same script, Python, dependencies, "
                    "port and health endpoint under a new process identity."
                ),
                "previous_identity_sha256": digest(rebound["previous"]),
                "current_identity_sha256": approval["service_identity_sha256"],
            },
        },
    }


def prepare(parent: Path, root: Path, approval_path: Path) -> dict:
    parent, root = parent.resolve(), root.resolve()
    if parent == root or root.is_relative_to(parent):
        raise ValueError("suffix must be outside its immutable parent")
    with ProcessLock(parent), ProcessLock(root):
        approval = read_json(approval_path)
        check_driver(approval)
        protocol = plan(parent, approval)
        claim_path = parent.with_name(parent.name + "-continuation.claim.json")
        claim = {"child": str(root), "approval_sha256": file_sha(approval_path)}
        if claim_path.exists() and read_json(claim_path) != claim:
            raise ValueError("parent already claimed")
        if (root / "protocol.json").exists():
            if read_json(root / "protocol.json") != protocol:
                raise ValueError("suffix protocol changed")
        else:
            write_json(claim_path, claim)
            write_json(root / "protocol.json", protocol)
            write_json(root / "protocol.sha256.json", {"sha256": digest(protocol)})
            write_json(
                root / "approval-identity.json", {"path": str(approval_path.resolve()), **claim}
            )
        return protocol


def terminal_unknown(segment: Path, row: dict, protocol: dict) -> dict:
    """Verify the already collected patch, never execute the agent again."""
    directory = segment / "trials" / row["id"]
    path = directory / "record.json"
    record = read_json(path)
    requests = validate_requests(segment, protocol)
    if (
        record.get("status") != "unknown_request"
        or not any(r["status"] != "completed" for r in requests)
        or read_json(segment / "requests.json").get("halt_reason")
    ):
        raise ValueError("only a durable provider unknown can be terminalized")
    raw = directory / "agent-unknown.json"
    if not raw.exists():
        shutil.copyfile(path, raw)
    elif read_json(raw).get("status") != "unknown_request":
        raise ValueError("unknown evidence changed")
    patch = directory / "patch.diff"
    record["patch_sha256"] = file_sha(patch) if patch.exists() else None
    record["terminal_provider_unknown"] = True
    result = finalize_trial(directory, record, protocol["tasks"][row["task_id"]])
    if str(result.get("verification", {}).get("reason", "")).startswith("infrastructure:"):
        raise ValueError("unknown patch verification infrastructure failed")
    return result


def run(root: Path, env_file: Path | None, *, max_trials: int = 60) -> list[dict]:
    with ProcessLock(root):
        protocol = read_json(root / "protocol.json")
        identity = read_json(root / "approval-identity.json")
        approval_path = Path(identity["path"])
        approval = read_json(approval_path)
        check_driver(approval)
        parent = Path(approval["parent_campaign"])
        if (
            identity["child"] != str(root.resolve())
            or file_sha(approval_path) != identity["approval_sha256"]
            or read_json(parent.with_name(parent.name + "-continuation.claim.json"))
            != {k: identity[k] for k in ("child", "approval_sha256")}
        ):
            raise ValueError("suffix claim changed")
        with ProcessLock(parent):
            expected = plan(parent, approval)
        if (
            protocol != expected
            or digest(protocol) != read_json(root / "protocol.sha256.json")["sha256"]
        ):
            raise ValueError("suffix identity changed")
        if protocol["mode"] == "live":
            load_environment_file(env_file)
        results = []
        previous_root, previous_protocol = parent, read_json(parent / "protocol.json")
        for row in protocol["schedule"]:
            if len(results) >= max_trials:
                break
            segment = root / "segments" / row["id"]
            segment_protocol = {
                **protocol,
                "tasks": {row["task_id"]: protocol["tasks"][row["task_id"]]},
                "schedule": [row],
                "funding": funding(previous_root, previous_protocol),
            }
            if (segment / "protocol.json").exists():
                if read_json(segment / "protocol.json") != segment_protocol:
                    raise ValueError("sealed segment identity changed")
            else:
                block = root / "blocks" / f"{row['block']:02d}.json"
                members = [r["id"] for r in protocol["schedule"] if r["block"] == row["block"]]
                block_record = {"protocol_sha256": digest(protocol), "trials": members}
                if block.exists():
                    if read_json(block) != block_record:
                        raise ValueError("block reservation changed")
                else:
                    if protocol["mode"] == "live" and not wallet(root, row["block"]):
                        write_json(
                            root / "wallet-stop.json", {"sent": False, "block": row["block"]}
                        )
                        break
                    remaining = 300 - segment_protocol["funding"]["prior_conservative_cny"]
                    reserve = len(members) * protocol_profile(protocol)["trial_limit_cny"]
                    if round(remaining * 1e6) < round(reserve * 1e6):
                        write_json(
                            root / "budget-stop.json", {"sent": False, "remaining_cny": remaining}
                        )
                        break
                    write_json(block, block_record)
                write_json(segment / "protocol.json", segment_protocol)
                write_json(segment / "protocol.sha256.json", {"sha256": digest(segment_protocol)})
                write_json(
                    segment / "requests.json",
                    {
                        "schema_version": 1,
                        "protocol_sha256": digest(segment_protocol),
                        "limit_cny": 300,
                        "requests": [],
                        **(
                            {
                                "supplier_system_fingerprint": read_json(parent / "requests.json")[
                                    "supplier_system_fingerprint"
                                ]
                            }
                            if "supplier_system_fingerprint" in read_json(parent / "requests.json")
                            else {}
                        ),
                    },
                )
            with ProcessLock(segment):
                path = segment / "trials" / row["id"] / "record.json"
                if path.exists():
                    result = read_json(path)
                    if not result.get("finished"):
                        if result.get("status") == "unknown_request":
                            result = terminal_unknown(segment, row, segment_protocol)
                        elif result.get("phase") == "agent_completed":
                            result = finalize_trial(
                                path.parent, result, segment_protocol["tasks"][row["task_id"]]
                            )
                        else:
                            raise ValueError("incomplete tools or agent; no replay")
                else:
                    if validate_requests(segment, segment_protocol):
                        raise ValueError("request without trial; no replay")
                    try:
                        result = execute_trial(segment, segment_protocol, row, env_file)
                    except ValueError as exc:
                        if str(exc) != "unknown provider result; campaign halted":
                            raise
                        result = terminal_unknown(segment, row, segment_protocol)
                validate_record(result, path.parent, row)
                if result.get("infrastructure_failure") and not result.get(
                    "terminal_provider_unknown"
                ):
                    write_json(segment / "halt.json", {"reason": "infrastructure failure"})
                    raise ValueError("non-provider infrastructure failure")
                funding(segment, segment_protocol)
            results.append(result)
            previous_root, previous_protocol = segment, segment_protocol
            print(
                {"id": row["id"], "status": result["status"], "passed": result["passed"]},
                flush=True,
            )
        write_json(
            root / "progress.json",
            {
                "attempted": len(results),
                "planned": len(protocol["schedule"]),
                "sealed_parent": str(previous_root),
            },
        )
        return results


def make_report(root: Path) -> dict:
    """Copy raw evidence for the unchanged reporter; never rewrite sealed ledgers."""
    with ProcessLock(root):
        protocol = read_json(root / "protocol.json")
        requests = []
        for row in protocol["schedule"]:
            segment = root / "segments" / row["id"]
            if not (segment / "requests.json").exists():
                continue
            ledger = validate_requests(segment, read_json(segment / "protocol.json"))
            source = segment / "trials" / row["id"]
            if source.exists():
                shutil.copytree(source, root / "trials" / row["id"], dirs_exist_ok=True)
            provider = segment / "provider"
            if provider.exists():
                shutil.copytree(provider, root / "provider", dirs_exist_ok=True)
            requests.extend(ledger)
        write_json(
            root / "requests.json",
            {"protocol_sha256": digest(protocol), "limit_cny": 300, "requests": requests},
        )
    return report(root)


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
        prepare(args.parent, args.campaign_dir, args.approval)
    elif args.action == "run":
        run(args.campaign_dir, args.env_file, max_trials=args.max_trials)
    else:
        print(make_report(args.campaign_dir))


if __name__ == "__main__":
    main()
