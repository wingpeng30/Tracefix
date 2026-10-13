"""Run the separately authorized B-only repetition of the frozen 30-task set.

The evaluator and tool loop are imported from the already validated e602 engine.
Only this campaign driver adds a one-arm schedule, an independent CNY ledger,
and a wallet-aware cap. It never replays provider requests with unknown results.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener


REPO = Path(__file__).resolve().parents[1]
FROZEN_ENGINE_SRC = Path(r"C:\Users\PengZixuan\.codex\worktrees\ac-thirty-frozen\Tracefix\src")
PARENT = Path(r"E:\TracefixExperiments\20261009-ac-30-180s\completion-campaign")
ANCHOR = PARENT.parent / "campaign"
CAMPAIGN = Path(r"E:\TracefixExperiments\20261010-b-30-180s")
PROFILE = "b-30-180s-v1"
LIMIT_CNY = 300.0
TASK_LIMIT_CNY = 10.0
TASK_SECONDS = 3600
REQUEST_SECONDS = 180
TOKENIZER = Path(r"D:\Tracefix\tmp\bc-v41-tokenizer.json")
ENV_FILE = REPO / ".env"
SELECTION = ANCHOR / "task-selection.json"
PARENT_PROGRESS = PARENT / "progress.json"


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_sha(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_engine():
    # The parent campaign pins the implementation by a source-tree digest.
    if not FROZEN_ENGINE_SRC.is_dir():
        raise RuntimeError("frozen engine worktree is unavailable")
    sys.path.insert(0, str(FROZEN_ENGINE_SRC))
    import tracefix.comparison_profiles as cp

    original_profile_config = cp.profile_config
    b_profile = {
        "name": PROFILE,
        "arms": ["B"],
        "repetitions": 1,
        "limits": {
            "input_tokens": 9223372036854775807,
            "output_tokens": 9223372036854775807,
            "requests": 2147483647,
            "tests": 2147483647,
            "active_seconds": TASK_SECONDS,
            "timeout_seconds": REQUEST_SECONDS,
            "limit_cny": LIMIT_CNY,
        },
        "per_request_output_tokens": 393216,
        "arm_limit_cny": None,
        "trial_limit_cny": TASK_LIMIT_CNY,
        "input_counter": "official_estimate",
        "capability_mode": True,
        "hard_request_deadline_seconds": REQUEST_SECONDS,
        "task_count": 30,
        "funding_mode": "independent_authorization",
    }

    def profile_config(name=cp.LEGACY_PROFILE):
        if name == PROFILE:
            return json.loads(json.dumps(b_profile))
        return original_profile_config(name)

    cp.profile_config = profile_config
    cp.HOLDOUT_PROFILES = tuple(cp.HOLDOUT_PROFILES) + (PROFILE,)

    import tracefix.comparison_funding as funding
    from tracefix.comparison import digest, file_sha, read_json
    from tracefix.comparison_campaign import (
        execute_trial,
        finalize_trial,
        git,
        validate_record,
        validate_requests,
    )
    from tracefix.comparison_profiles import OfficialCounter, protocol_profile

    original_prior_spend = funding.prior_spend

    def prior_spend(protocol: dict) -> float:
        f = protocol.get("funding") or {}
        if f.get("kind") == "b_independent_authorization":
            auth = Path(f["authorization"])
            record = read_json(auth)
            claim = read_json(auth.with_suffix(".claim.json"))
            if (
                protocol.get("profile", {}).get("name") != PROFILE
                or file_sha(auth) != f["authorization_sha256"]
                or record.get("kind") != "tracefix_paid_authorization"
                or record.get("profile") != PROFILE
                or record.get("limit_cny") != LIMIT_CNY
                or record.get("authorization_id") != f["authorization_id"]
                or record.get("authorization_path") != str(auth.resolve())
                or f.get("prior_conservative_cny") != 0.0
                or claim
                != {
                    "campaign": f["campaign"],
                    "authorization_sha256": f["authorization_sha256"],
                }
                or canonical_sha(claim) != f["claim_sha256"]
            ):
                raise ValueError("B authorization identity changed")
            return 0.0
        if f.get("kind") == "b_chain":
            parent = Path(f["parent_campaign"])
            if (
                file_sha(parent / "requests.json") != f["requests_sha256"]
                or file_sha(parent / "protocol.json") != f["parent_protocol_sha256"]
                or read_json(parent / "protocol.json").get("profile", {}).get("name") != PROFILE
                or not 0 <= float(f["prior_conservative_cny"]) <= LIMIT_CNY
            ):
                raise ValueError("B liability chain changed")
            return float(f["prior_conservative_cny"])
        return original_prior_spend(protocol)

    funding.prior_spend = prior_spend
    from tracefix.comparison import ComparisonBudget
    from tracefix.comparison_campaign import ComparisonBudget as CampaignBudget

    class WalletBoundBudget(CampaignBudget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            protocol = read_json(self.root / "protocol.json")
            cap = protocol.get("wallet_cap_cny")
            if not isinstance(cap, (int, float)) or not 0 < cap <= TASK_LIMIT_CNY:
                raise ValueError("wallet-specific task fee cap missing")
            # protocol_profile returns the frozen nested dict by reference; copy it
            # before applying this trial's account-balance fee cap.
            self.profile = dict(self.profile)
            self.profile["arm_limit_cny"] = float(cap)

    # Keep protocol_profile's version check, but direct the execution adapter to
    # the wallet-bound subclass without changing simple_loop or B policy.
    import tracefix.comparison_campaign as campaign

    campaign.ComparisonBudget = WalletBoundBudget
    from tracefix import TraceFixRunner

    return {
        "cp": cp,
        "profile": b_profile,
        "digest": digest,
        "file_sha": file_sha,
        "read_json": read_json,
        "write_json": write,
        "execute_trial": execute_trial,
        "finalize_trial": finalize_trial,
        "git": git,
        "validate_record": validate_record,
        "validate_requests": validate_requests,
        "OfficialCounter": OfficialCounter,
        "protocol_profile": protocol_profile,
        "TraceFixRunner": TraceFixRunner,
    }


ENGINE = load_engine()


def price_snapshot() -> dict:
    # Verified against the live official documentation at experiment freeze.
    return {
        "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        "verified_at": now(),
        "model_alias": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "base_url": "https://api.deepseek.com",
        "thinking": "disabled",
        "context_tokens": 1048576,
        "max_output_tokens": 393216,
        "peak_cny_per_million": {"input_cache_miss": 2.0, "input_cache_hit": 0.04, "output": 8.0},
        "off_peak_cny_per_million": {
            "input_cache_miss": 1.0,
            "input_cache_hit": 0.02,
            "output": 4.0,
        },
        "reservation_rates": {"input": 2.0, "output": 8.0},
        "note": "Reservations use peak cache-miss input and peak output rates.",
    }


def wallet_snapshot(root: Path, index: int) -> dict:
    request = Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"]},
    )
    with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
        balance = json.loads(response.read())
    balance["observed_at"] = now()
    write(root / "wallet" / f"{index:02d}.json", balance)
    cny = next(row for row in balance["balance_infos"] if row["currency"] == "CNY")
    balance["available_cny"] = float(cny["total_balance"])
    return balance


def funding_liability(root: Path) -> tuple[float, float, float]:
    known = unknown = 0.0
    for segment in sorted((root / "segments").glob("[0-9][0-9][0-9]")):
        path = segment / "requests.json"
        if not path.exists():
            continue
        for row in read(path).get("requests", []):
            if "peak_cost_cny" in row:
                known += float(row["peak_cost_cny"])
            elif row.get("status") != "completed":
                unknown += float(row.get("reserved_peak_cny", 0.0))
    return known, unknown, known + unknown


def source_check(engine, previous: dict, parent: dict) -> tuple[list[str], dict]:
    from tracefix.models.input_bounds import V41_TOKENIZER_SHA256

    parent_path = PARENT / "protocol.json"
    if engine["digest"](previous) != read(PARENT / "protocol.sha256.json")["sha256"]:
        raise ValueError("previous campaign protocol hash mismatch")
    if previous.get("profile", {}).get("name") != "ac-30-180s-v1":
        raise ValueError("expected frozen A/C campaign protocol")
    selection = read(SELECTION)
    task_ids = selection.get("selected_task_ids", [])
    if len(task_ids) != 30 or len(set(task_ids)) != 30 or set(task_ids) != set(previous["tasks"]):
        raise ValueError("frozen thirty-task selection changed")
    if selection.get("kind") != "tracefix_ac_thirty_selection":
        raise ValueError("selection receipt identity mismatch")
    if previous.get("implementation_sha256") != engine["TraceFixRunner"]._implementation_sha256():
        raise ValueError("validated engine implementation identity mismatch")
    if previous.get("product_base") != "e380329d2a007a83dd317944bafd426d4b829c82":
        raise ValueError("product baseline changed")
    runtime = previous.get("counter_runtime") or {}
    if not TOKENIZER.is_file() or sha(TOKENIZER) != V41_TOKENIZER_SHA256:
        raise ValueError("pinned official tokenizer file mismatch")
    if runtime.get("identity", {}).get("tokenizer_sha256") != V41_TOKENIZER_SHA256:
        raise ValueError("pinned official tokenizer runtime mismatch")
    from tracefix.comparison_transport import transport_identity

    current_transport = transport_identity()
    old_transport = previous.get("provider_transport", {})
    if (
        current_transport["command"] != old_transport.get("command")
        or current_transport["interpreter_sha256"] != old_transport.get("interpreter_sha256")
    ):
        raise ValueError("provider worker interpreter identity changed")
    materials = {}
    for task_id in task_ids:
        spec = previous["tasks"][task_id]
        prompt = Path(spec["prompt_path"])
        source = Path(spec["source"])
        python = Path(spec["python"])
        if not prompt.is_file() or sha(prompt) != spec["prompt_sha256"]:
            raise ValueError(f"frozen prompt changed: {task_id}")
        if not source.is_dir() or not python.is_file():
            raise ValueError(f"task environment unavailable: {task_id}")
        commit = engine["git"](source, "rev-parse", "HEAD")
        if commit != spec["source_identity"].get("commit"):
            raise ValueError(f"task source base changed: {task_id}")
        # Reuse the prior qualification fingerprint while checking the interpreter
        # and required test runner are still usable; no task tests are rerun.
        probe = subprocess.run(
            [str(python), "-c", "import pytest,sys; print(sys.version.split()[0])"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        materials[task_id] = {
            "prompt_sha256": spec["prompt_sha256"],
            "source_commit": commit,
            "environment_identity": spec["environment_identity"],
            "python_version_probe": probe.stdout.strip(),
        }
    previous_service = next(
        spec["service_identity"]
        for spec in previous["tasks"].values()
        if spec.get("service_identity")
    )
    for spec in previous["tasks"].values():
        if spec.get("service_identity") != previous_service:
            raise ValueError("task service identities diverge")
    service_path = CAMPAIGN / "httpbin-identity.json"
    if not service_path.is_file():
        raise ValueError("restarted controlled service identity is missing")
    service = read(service_path)
    mutable = {"pid", "started_at", "startup_nonce", "identity_url"}
    if any(previous_service.get(key) != service.get(key) for key in previous_service if key not in mutable):
        raise ValueError("restarted controlled service build or environment changed")
    if service.get("port") != previous_service.get("port") or service.get("health_url") != previous_service.get("health_url"):
        raise ValueError("restarted controlled service endpoint changed")
    opener = build_opener(ProxyHandler({}))
    with opener.open(service["identity_url"], timeout=5) as response:
        if json.load(response) != service:
            raise ValueError("controlled test service identity mismatch")
    with opener.open(service["health_url"], timeout=5) as response:
        if json.load(response).get("url") != service["health_url"]:
            raise ValueError("controlled test service unavailable")
    # Exercise the pinned official template/tokenizer locally, without a provider call.
    counter = ENGINE["OfficialCounter"](runtime)
    test_body = {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "TraceFix tokenizer identity probe"}],
        "tools": [],
        "thinking": {"type": "disabled"},
    }
    counted = counter.invoke([test_body])
    if counted.get("identity") != runtime.get("identity"):
        raise ValueError("official tokenizer runtime identity mismatch")
    return task_ids, {
        "selection_sha256": sha(SELECTION),
        "parent_protocol_sha256": sha(parent_path),
        "tokenizer_sha256": sha(TOKENIZER),
        "tokenizer_probe": counted,
        "task_materials": materials,
        "service_identity_sha256": canonical_sha(service),
        "previous_service_identity_sha256": canonical_sha(previous_service),
        "service_identity": service,
        "provider_transport": current_transport,
    }


def prepare(root: Path) -> dict:
    if root.resolve() != CAMPAIGN.resolve():
        raise ValueError(f"campaign output must be {CAMPAIGN}")
    root.mkdir(parents=True, exist_ok=True)
    if (root / "protocol.json").exists():
        raise ValueError("B protocol already frozen; use run to resume")
    if not ENV_FILE.is_file():
        raise ValueError("DeepSeek environment file unavailable")
    from tracefix.runtime import load_environment_file

    load_environment_file(ENV_FILE)
    previous = read(PARENT / "protocol.json")
    anchor = read(ANCHOR / "protocol.json")
    task_ids, checked = source_check(ENGINE, previous, anchor)
    selection = read(SELECTION)
    anchor_protocol = read(ANCHOR / "protocol.json")
    blocks = {
        row["task_id"]: row["block"]
        for row in anchor_protocol["schedule"]
        if row.get("task_id") in task_ids
    }
    # Task order is the sealed task-selection order used by the A/C campaign.
    rows = [
        {"id": f"{index:03d}", "task_id": task_id, "repetition": 1, "arm": "B", "block": index}
        for index, task_id in enumerate(task_ids, 1)
    ]
    rows = sorted(rows, key=lambda row: (blocks.get(row["task_id"], 10_000), row["id"]))
    for index, row in enumerate(rows, 1):
        row["id"] = f"{index:03d}"
        row["block"] = index

    auth = root / "authorization.json"
    auth_record = {
        "kind": "tracefix_paid_authorization",
        "profile": PROFILE,
        "limit_cny": LIMIT_CNY,
        "authorization_id": "20261010-b-30-180s-" + uuid.uuid4().hex,
        "authorization_path": str(auth.resolve()),
        "user_instruction": "用户明确授权：同样三十题B组各一次，独立人民币三百元账本；使用实际余额，资金不足即停止。",
        "created_at": now(),
    }
    write(auth, auth_record)
    claim = {"campaign": str(root.resolve()), "authorization_sha256": sha(auth)}
    write(auth.with_suffix(".claim.json"), claim)
    funding = {
        "kind": "b_independent_authorization",
        "authorization_cny": LIMIT_CNY,
        "prior_conservative_cny": 0.0,
        "authorization": str(auth.resolve()),
        "authorization_sha256": sha(auth),
        "authorization_id": auth_record["authorization_id"],
        "campaign": str(root.resolve()),
        "claim_sha256": canonical_sha(claim),
    }
    price = price_snapshot()
    write(root / "prices.json", price)
    model_request = Request(
        "https://api.deepseek.com/models",
        headers={"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"]},
    )
    with build_opener(ProxyHandler({})).open(model_request, timeout=30) as response:
        model_catalog = json.loads(response.read())
    write(root / "provider-models.json", model_catalog)
    if not any(item.get("id") == "deepseek-flash" for item in model_catalog.get("data", [])):
        raise RuntimeError("deepseek-flash not present in authenticated provider model list")
    bal = wallet_snapshot(root, 0)
    if not bal.get("is_available") or bal["available_cny"] <= 0:
        write(root / "wallet-stop.json", {"sent": False, "reason": "wallet unavailable or empty"})
        raise RuntimeError("DeepSeek wallet is unavailable or empty; no paid call sent")
    protocol = {
        "schema_version": 1,
        "experiment": "later B-only repeat of frozen A/C thirty-task campaign",
        "profile": ENGINE["profile"],
        "mode": "live",
        "product_base": previous["product_base"],
        "implementation_commit": previous["implementation_commit"],
        "implementation_sha256": previous["implementation_sha256"],
        "driver_path": str(Path(__file__).resolve()),
        "driver_sha256": sha(Path(__file__).resolve()),
        "parent_campaign": str(PARENT.resolve()),
        "parent_protocol_sha256": sha(PARENT / "protocol.json"),
        "parent_summary_sha256": sha(PARENT / "summary.json"),
        "parent_per_task_sha256": sha(PARENT / "per-task.csv"),
        "selection_path": str(SELECTION.resolve()),
        "selection_sha256": checked["selection_sha256"],
        "artifacts": {
            "tokenizer_calibration": sha(ANCHOR / "tokenizer-calibration.json"),
            "qualification_gate": sha(PARENT.parent / "continuation-prepaid-gates.json"),
            "engine_ci": sha(PARENT.parent / "ci-evidence-e602c74.json"),
        },
        "counter_runtime": previous["counter_runtime"],
        "provider_transport": checked["provider_transport"],
        "funding": funding,
        "model": price,
        "budgets": {
            "authorization_cny": LIMIT_CNY,
            "task_max_cny": TASK_LIMIT_CNY,
            "active_seconds": TASK_SECONDS,
            "request_timeout_seconds": REQUEST_SECONDS,
            "cumulative_input_tokens": "unlimited except provider context and wallet/cost budget",
            "cumulative_output_tokens": "unlimited except provider context and wallet/cost budget",
            "request_count": "unlimited except active execution, provider and wallet/cost budget",
            "test_count": "unlimited except active execution, provider and wallet/cost budget",
            "retry_count": 0,
        },
        "task_order": task_ids,
        "schedule": rows,
        "tasks": {
            task_id: {**spec, "service_identity": checked["service_identity"]}
            for task_id, spec in previous["tasks"].items()
        },
        "qualification_reused": True,
        "qualification_note": "Reused previously accepted qualification and environment receipts; startup only checked frozen prompts, source commits, interpreter availability and service/tokenizer identity. No offline task tests rerun.",
        "comparison_note": "B is a later follow-up run and was not concurrent or randomized against existing C.",
        "frozen_at": now(),
    }
    if protocol["driver_sha256"] != sha(Path(__file__).resolve()):
        raise ValueError("driver changed while freezing")
    write(root / "protocol.json", protocol)
    write(root / "protocol.sha256.json", {"sha256": canonical_sha(protocol)})
    write(root / "startup-gate.json", {
        "accepted": True,
        "paid_calls_before_gate": 0,
        "balance_snapshot": str((root / "wallet" / "00.json").resolve()),
        "balance_cny": bal["available_cny"],
        "provider_models_sha256": sha(root / "provider-models.json"),
        "price_source": price["source"],
        "task_count": len(rows),
        "task_material_hashes": checked["task_materials"],
        "service_rebind": {
            "previous_identity_sha256": checked["previous_service_identity_sha256"],
            "current_identity_sha256": checked["service_identity_sha256"],
            "reason": "Restarted the same pinned service build and dependency set after the previous process exited.",
        },
        "tokenizer_probe_identity": canonical_sha(checked["tokenizer_probe"].get("identity")),
        "provider_endpoint": "https://api.deepseek.com",
        "model_alias": "deepseek-flash",
        "created_at": now(),
    })
    write(root / "storage-owner.json", {
        "owner": "TraceFix 2026-10-10 B-only paid experiment",
        "campaign": str(root.resolve()),
        "contains": "Raw requests, responses, workspaces, patches, verification evidence and wallet snapshots.",
        "cleanup_after_review": True,
        "cleanup_command": f"Remove-Item -LiteralPath '{root.resolve()}' -Recurse -Force",
        "created_at": now(),
    })
    write(root / "progress.json", {"started": 0, "planned": 30, "known_fee_cny": 0.0, "unknown_reserve_cny": 0.0})
    return protocol


def trial_funding(root: Path, prior_segment: Path | None, prior_protocol: dict) -> dict:
    known, unknown, total = funding_liability(root)
    if total > LIMIT_CNY or not 0 <= total:
        raise ValueError("B authorization reserve exhausted")
    if prior_segment is None:
        f = read(root / "protocol.json")["funding"] if "funding" in read(root / "protocol.json") else None
        # First-trial funding is copied from the already claimed campaign authorization.
        auth = root / "authorization.json"
        claim = read(auth.with_suffix(".claim.json"))
        return {
            "kind": "b_independent_authorization",
            "authorization_cny": LIMIT_CNY,
            "prior_conservative_cny": 0.0,
            "authorization": str(auth.resolve()),
            "authorization_sha256": sha(auth),
            "authorization_id": read(auth)["authorization_id"],
            "campaign": str(root.resolve()),
            "claim_sha256": canonical_sha(claim),
        }
    return {
        "kind": "b_chain",
        "parent_campaign": str(prior_segment.resolve()),
        "requests_sha256": sha(prior_segment / "requests.json"),
        "parent_protocol_sha256": sha(prior_segment / "protocol.json"),
        "prior_conservative_cny": total,
        "known_cny": known,
        "unknown_reserve_cny": unknown,
        "authorization_cny": LIMIT_CNY,
    }


def finish_unknown(engine, segment: Path, row: dict, protocol: dict) -> dict:
    directory = segment / "trials" / row["id"]
    path = directory / "record.json"
    record = read(path)
    requests = engine["validate_requests"](segment, protocol)
    if record.get("status") != "unknown_request" or not any(r["status"] != "completed" for r in requests):
        raise ValueError("unknown provider state is not durable")
    if not (directory / "agent-unknown.json").exists():
        import shutil

        shutil.copyfile(path, directory / "agent-unknown.json")
    patch = directory / "patch.diff"
    record["patch_sha256"] = sha(patch) if patch.exists() else None
    record["terminal_provider_unknown"] = True
    result = engine["finalize_trial"](directory, record, protocol["tasks"][row["task_id"]])
    if str(result.get("verification", {}).get("reason", "")).startswith("infrastructure:"):
        raise ValueError("independent verification infrastructure failure")
    return result


def run(root: Path, max_trials: int = 30) -> list[dict]:
    protocol = read(root / "protocol.json")
    if ENGINE["file_sha"](Path(protocol["driver_path"])) != protocol["driver_sha256"]:
        raise ValueError("frozen B scheduling implementation changed")
    if canonical_sha(protocol) != read(root / "protocol.sha256.json")["sha256"]:
        raise ValueError("B campaign protocol hash changed")
    if protocol["profile"] != ENGINE["profile"]:
        raise ValueError("B profile changed")
    from tracefix.runtime import load_environment_file

    load_environment_file(ENV_FILE)
    results = []
    previous_segment = None
    for row in protocol["schedule"]:
        if len(results) >= max_trials:
            break
        segment = root / "segments" / row["id"]
        record_path = segment / "trials" / row["id"] / "record.json"
        if record_path.exists():
            existing = read(record_path)
            if not existing.get("finished"):
                if existing.get("status") == "unknown_request":
                    local_protocol = read(segment / "protocol.json")
                    result = finish_unknown(ENGINE, segment, row, local_protocol)
                elif existing.get("phase") == "agent_completed":
                    local_protocol = read(segment / "protocol.json")
                    result = ENGINE["finalize_trial"](
                        record_path.parent, existing, local_protocol["tasks"][row["task_id"]]
                    )
                else:
                    raise ValueError("incomplete non-provider task cannot be replayed")
            else:
                result = existing
            if not result.get("finished"):
                raise ValueError("existing trial not finalized")
            results.append(result)
            previous_segment = segment
            update_progress(root, protocol)
            continue

        if (segment / "requests.json").exists():
            raise ValueError("request ledger exists without a task record; no replay")
        bal = wallet_snapshot(root, row["block"])
        known, unknown, prior = funding_liability(root)
        effective_wallet = bal.get("available_cny", 0.0) - unknown
        remaining_auth = LIMIT_CNY - prior
        if not bal.get("is_available") or effective_wallet <= 0:
            write(root / "wallet-stop.json", {"sent": False, "block": row["block"], "balance_cny": bal.get("available_cny"), "unknown_reserve_cny": unknown})
            break
        if remaining_auth <= 0:
            write(root / "budget-stop.json", {"sent": False, "block": row["block"], "liability_cny": prior})
            break
        wallet_cap = min(TASK_LIMIT_CNY, effective_wallet, remaining_auth)
        if wallet_cap <= 0:
            break
        local_protocol = {
            **protocol,
            "tasks": {row["task_id"]: protocol["tasks"][row["task_id"]]},
            "schedule": [row],
            "funding": trial_funding(root, previous_segment, {}),
            "wallet_cap_cny": wallet_cap,
            "wallet_snapshot_sha256": sha(root / "wallet" / f"{row['block']:02d}.json"),
        }
        segment.mkdir(parents=True, exist_ok=True)
        write(segment / "protocol.json", local_protocol)
        write(segment / "protocol.sha256.json", {"sha256": canonical_sha(local_protocol)})
        write(segment / "requests.json", {
            "schema_version": 1,
            "protocol_sha256": canonical_sha(local_protocol),
            "limit_cny": LIMIT_CNY,
            "requests": [],
        })
        try:
            result = ENGINE["execute_trial"](segment, local_protocol, row, ENV_FILE)
        except ValueError as exc:
            if str(exc) != "unknown provider result; campaign halted":
                raise
            result = finish_unknown(ENGINE, segment, row, local_protocol)
        requests = ENGINE["validate_requests"](segment, local_protocol)
        if any(r.get("official_count_delta") != 0 for r in requests if r.get("status") == "completed"):
            write(segment / "halt.json", {"reason": "tokenizer usage discrepancy"})
            raise ValueError("tokenizer count differs from supplier usage; paid execution halted")
        ledger = read(segment / "requests.json")
        if ledger.get("halt_reason"):
            # The provider result is already durable. Do not continue on an identity,
            # count, or generation anomaly.
            write(segment / "halt.json", {"reason": ledger["halt_reason"]})
            raise ValueError("provider/usage anomaly; campaign halted")
        ENGINE["validate_record"](result, record_path.parent, row)
        if result.get("infrastructure_failure") and not result.get("terminal_provider_unknown"):
            write(segment / "halt.json", {"reason": "independent verification infrastructure failure"})
            raise ValueError("verification infrastructure failure; campaign halted")
        known, unknown, liability = funding_liability(root)
        write(segment / "settlement.json", {
            "task_id": row["task_id"],
            "known_peak_cny": known,
            "unknown_reserved_cny": unknown,
            "total_conservative_liability_cny": liability,
            "authorization_cny": LIMIT_CNY,
            "account_balance_after": bal.get("available_cny"),
            "settled_at": now(),
        })
        results.append(result)
        previous_segment = segment
        update_progress(root, protocol)
        print(json.dumps({
            "completed": len(results), "planned": 30, "task": row["task_id"],
            "status": result.get("status"), "passed": result.get("passed"),
            "successes": sum(1 for item in results if item.get("passed") and item.get("status") == "completed"),
            "known_peak_cny": round(known, 6), "unknown_reserved_cny": round(unknown, 6),
            "wallet_cny_snapshot": bal.get("available_cny"),
        }, ensure_ascii=False), flush=True)
        # Unknown responses are terminal failures; the held reservation remains in
        # the liability chain and the next task gets its own independent ledger.
    return results


def update_progress(root: Path, protocol: dict) -> None:
    rows = []
    known = unknown = 0.0
    for row in protocol["schedule"]:
        rec = root / "segments" / row["id"] / "trials" / row["id"] / "record.json"
        if rec.exists():
            record = read(rec)
            rows.append(record)
        ledger_path = root / "segments" / row["id"] / "requests.json"
        if ledger_path.exists():
            for request in read(ledger_path).get("requests", []):
                if "peak_cost_cny" in request:
                    known += float(request["peak_cost_cny"])
                elif request.get("status") != "completed":
                    unknown += float(request.get("reserved_peak_cny", 0.0))
    started = len(rows)
    write(root / "progress.json", {
        "started": started,
        "planned": len(protocol["schedule"]),
        "main_successes": sum(1 for row in rows if row.get("status") == "completed" and row.get("passed")),
        "patch_passes": sum(1 for row in rows if row.get("verification", {}).get("passed")),
        "known_peak_fee_cny": round(known, 6),
        "unknown_reserved_cny": round(unknown, 6),
        "updated_at": now(),
    })


def report(root: Path) -> dict:
    protocol = read(root / "protocol.json")
    rows = []
    known = unknown = inputs = outputs = 0.0
    for row in protocol["schedule"]:
        segment = root / "segments" / row["id"]
        path = segment / "trials" / row["id"] / "record.json"
        record = read(path) if path.exists() else None
        reqs = read(segment / "requests.json").get("requests", []) if (segment / "requests.json").exists() else []
        cost = in_tok = out_tok = 0
        held = 0.0
        for request in reqs:
            if "peak_cost_cny" in request:
                cost += request["peak_cost_cny"]
                in_tok += request.get("usage", {}).get("input_tokens", 0)
                out_tok += request.get("usage", {}).get("output_tokens", 0)
            else:
                held += request.get("reserved_peak_cny", 0.0)
        known += cost
        unknown += held
        inputs += in_tok
        outputs += out_tok
        rows.append({
            "id": row["id"], "task_id": row["task_id"],
            "started": record is not None,
            "status": record.get("status") if record else "unstarted",
            "main_success": bool(record and record.get("status") == "completed" and record.get("passed")),
            "patch_pass": bool(record and record.get("verification", {}).get("passed")),
            "normal_completion": bool(record and record.get("status") == "completed"),
            "input_tokens": in_tok, "output_tokens": out_tok,
            "peak_cost_cny": cost, "unknown_reserve_cny": held,
            "seconds": record.get("timings", {}).get("delivery_seconds", record.get("seconds", 0)) if record else None,
            "reason": record.get("reason") if record else "not executed",
            "verification_reason": record.get("verification", {}).get("reason") if record else None,
        })
    started = sum(r["started"] for r in rows)
    successes = sum(r["main_success"] for r in rows)
    patch_passes = sum(r["patch_pass"] for r in rows)
    completion = sum(r["normal_completion"] for r in rows)
    spent_time = sum(r["seconds"] or 0 for r in rows)
    summary = {
        "profile": PROFILE,
        "protocol_sha256": canonical_sha(protocol),
        "planned": len(rows), "started": started, "unstarted": len(rows) - started,
        "B": {
            "main_successes": successes, "main_success_rate": successes / started if started else None,
            "patch_passes": patch_passes, "patch_pass_rate": patch_passes / started if started else None,
            "normal_completions": completion, "normal_completion_rate": completion / started if started else None,
            "input_tokens": int(inputs), "output_tokens": int(outputs),
            "known_peak_fee_cny": round(known, 6), "unknown_reserve_cny": round(unknown, 6),
            "conservative_liability_cny": round(known + unknown, 6),
            "total_delivery_seconds": round(spent_time, 3),
            "seconds_per_main_success": round(spent_time / successes, 3) if successes else None,
            "cost_per_main_success_cny": round((known + unknown) / successes, 6) if successes else None,
        },
        "comparison_note": "B is a later, separately run sample than C; not a randomized concurrent A/B/C run.",
        "c_task_data_source": str(PARENT / "per-task.csv"),
        "c_task_data_sha256": sha(PARENT / "per-task.csv"),
        "generated_at": now(),
        "tasks": rows,
    }
    write(root / "per-task.json", {"tasks": rows})
    write(root / "summary.json", summary)
    import csv

    with (root / "per-run.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["id"])
        writer.writeheader()
        writer.writerows(rows)
    write(root / "progress.json", {
        "started": started, "planned": len(rows), "main_successes": successes,
        "patch_passes": patch_passes, "known_peak_fee_cny": round(known, 6),
        "unknown_reserved_cny": round(unknown, 6), "updated_at": now(),
    })
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "report"))
    parser.add_argument("--campaign-dir", type=Path, default=CAMPAIGN)
    parser.add_argument("--max-trials", type=int, default=30)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.campaign_dir)
    elif args.action == "run":
        run(args.campaign_dir, max_trials=args.max_trials)
    else:
        print(json.dumps(report(args.campaign_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
