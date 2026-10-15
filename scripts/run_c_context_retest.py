"""Run the authorized C-only post-context-fix paid cache retest."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

REPO = Path(__file__).resolve().parents[1]
PARENT = Path(r"E:\TracefixExperiments\20261009-ac-30-180s\completion-campaign")
ANCHOR = PARENT.parent / "campaign"
OUTPUT = Path(r"E:\TracefixExperiments\20261010-c-context-retest-30-180s")
ANALYSIS = REPO / "runs" / "b-c-full30-analysis-20261010"
OLD_CSV = ANALYSIS / "per-run.csv"
OLD_CACHE_CSV = ANALYSIS / "cache-per-request.csv"
PROFILE_NAME = "ac-30-180s-v1"
PILOT_TASK = "pytest-dev__pytest-7432"
TOTAL_CNY = 300.0
TASK_CNY = 10.0
TASK_SECONDS = 3600
REQUEST_SECONDS = 180
EXPECTED_OLD_OVERALL = 0.2457
OVERALL_IMPROVEMENT = 0.10
FACT_HIT_FLOOR = 0.90
MIN_FACT_REQUESTS = 2
PRICE_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
MODEL_URL = "https://api.deepseek.com/models"
BALANCE_URL = "https://api.deepseek.com/user/balance"
ENV_FILE = REPO / ".env"


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def now() -> str:
    return datetime.now(UTC).isoformat()


def load_engine():
    # Bind execution to this checkout's fixed source digest, never the old C/B worktree.
    sys.path.insert(0, str(REPO / "src"))
    import tracefix.comparison_campaign as campaign
    from tracefix import TraceFixRunner
    from tracefix.checkpoint import ProcessLock
    from tracefix.comparison_campaign import finalize_trial, validate_record, validate_requests
    from tracefix.comparison_profiles import OfficialCounter, profile_config
    from tracefix.comparison_transport import transport_identity
    from tracefix.models.input_bounds import V41_TOKENIZER_SHA256

    class WalletBoundBudget(campaign.ComparisonBudget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            local = read_json(self.root / "protocol.json")
            cap = local.get("wallet_cap_cny")
            if not isinstance(cap, (int, float)) or not 0 < cap <= TASK_CNY:
                raise ValueError("wallet-specific task fee cap missing")
            self.profile = dict(self.profile)
            self.profile["arm_limit_cny"] = float(cap)

    campaign.ComparisonBudget = WalletBoundBudget
    return {
        "campaign": campaign,
        "TraceFixRunner": TraceFixRunner,
        "ProcessLock": ProcessLock,
        "finalize_trial": finalize_trial,
        "validate_record": validate_record,
        "validate_requests": validate_requests,
        "OfficialCounter": OfficialCounter,
        "profile": profile_config(PROFILE_NAME),
        "transport_identity": transport_identity,
        "tokenizer_sha": V41_TOKENIZER_SHA256,
    }


ENGINE = load_engine()


def source_manifest() -> dict:
    source = REPO / "src" / "tracefix"
    files = {
        path.relative_to(source).as_posix(): file_sha(path)
        for path in sorted(source.rglob("*.py"))
        if "__pycache__" not in path.parts
    }
    return {"files": files, "sha256": digest(files)}


def save_source_snapshot(root: Path) -> str:
    archive = root / "source-snapshot.zip"
    source = REPO / "src" / "tracefix"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(source.rglob("*.py")):
            if "__pycache__" not in path.parts:
                zf.write(path, Path("src/tracefix") / path.relative_to(source))
        zf.write(Path(__file__).resolve(), "scripts/run_c_context_retest.py")
        for name in ("pyproject.toml", "README.md", "docs/context-management.md"):
            path = REPO / name
            if path.is_file():
                zf.write(path, name)
    return file_sha(archive)


def api_json(url: str, key: str) -> dict:
    request = Request(url, headers={"Authorization": f"Bearer {key}"})
    with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
        return json.loads(response.read())


def wallet_snapshot(root: Path, index: int) -> dict:
    balance = api_json(BALANCE_URL, os.environ["DEEPSEEK_API_KEY"])
    row = next((r for r in balance.get("balance_infos", []) if r.get("currency") == "CNY"), None)
    if row is None:
        raise ValueError("provider balance response has no CNY balance")
    balance["available_cny"] = float(row["total_balance"])
    balance["observed_at"] = now()
    write_json(root / "wallet" / f"{index:02d}.json", balance)
    return balance


def old_pilot_baseline(previous: dict) -> dict:
    matches = []
    for folder in sorted((PARENT / "trials").glob("[0-9][0-9][0-9]")):
        path = folder / "record.json"
        if path.is_file():
            record = read_json(path)
            if record.get("task_id") == PILOT_TASK and record.get("arm") == "C":
                matches.append((record, PARENT / "segments" / record["id"]))
    if len(matches) != 1:
        raise ValueError("expected exactly one previous C record for pytest #7432")
    record, segment = matches[0]
    ledger = read_json(segment / "requests.json")
    rows = [r for r in ledger["requests"] if r.get("status") == "completed"]
    if len(rows) != 16 or any("supplier_raw_usage" not in r for r in rows):
        raise ValueError("previous pilot usage evidence is incomplete")
    input_tokens = sum(int(r["usage"]["input_tokens"]) for r in rows)
    hits = sum(int(r["supplier_raw_usage"]["prompt_cache_hit_tokens"]) for r in rows)
    rate = hits / input_tokens
    if abs(rate - EXPECTED_OLD_OVERALL) > 0.0001:
        raise ValueError(f"previous pilot cache rate changed: {rate:.6%}")
    # The cache diagnostic was emitted with a UTF-8 BOM; consume it explicitly so
    # DictReader exposes the first header as ``arm`` on Windows.
    with OLD_CACHE_CSV.open(encoding="utf-8-sig", newline="") as stream:
        fact_rows = [
            r
            for r in csv.DictReader(stream)
            if r["arm"] == "C"
            and r["task_id"] == PILOT_TASK
            and r["transition"] in {"fact_anchor_inserted", "fact_anchor_updated"}
        ]
    fact_input = sum(int(r["input_tokens"]) for r in fact_rows)
    fact_hits = sum(int(r["hit_tokens"]) for r in fact_rows)
    fact_rate = fact_hits / fact_input if fact_input else 0
    if len(fact_rows) != 11 or abs(fact_rate - 0.0041) > 0.001:
        raise ValueError("previous pilot fact-anchor evidence differs from frozen baseline")
    return {
        "old_trial_id": record["id"],
        "old_status": record["status"],
        "known_requests": len(rows),
        "input_tokens": input_tokens,
        "cache_hit_tokens": hits,
        "overall_hit_rate": rate,
        "fact_request_count": len(fact_rows),
        "fact_input_tokens": fact_input,
        "fact_hit_tokens": fact_hits,
        "fact_hit_rate": fact_rate,
        "request_ledger_sha256": file_sha(segment / "requests.json"),
        "per_run_sha256": file_sha(OLD_CSV),
        "cache_diagnostic_sha256": file_sha(OLD_CACHE_CSV),
    }


def verify_inputs(previous: dict, task_ids: list[str]) -> tuple[dict, dict]:
    from tracefix.comparison_campaign import git

    materials = {}
    for task_id in task_ids:
        spec = previous["tasks"][task_id]
        prompt, source, python = (
            Path(spec["prompt_path"]),
            Path(spec["source"]),
            Path(spec["python"]),
        )
        if not prompt.is_file() or file_sha(prompt) != spec["prompt_sha256"]:
            raise ValueError(f"frozen prompt changed: {task_id}")
        if not source.is_dir() or not python.is_file():
            raise ValueError(f"task source or interpreter unavailable: {task_id}")
        if git(source, "rev-parse", "HEAD") != spec["source_identity"]["commit"]:
            raise ValueError(f"task source commit changed: {task_id}")
        materials[task_id] = {
            "prompt_sha256": file_sha(prompt),
            "source_commit": spec["source_identity"]["commit"],
            "environment_identity": spec["environment_identity"],
            "python_executable": str(python),
        }
    runtime = previous.get("counter_runtime") or {}
    tokenizer = Path(r"D:\Tracefix\tmp\bc-v41-tokenizer.json")
    if not tokenizer.is_file() or file_sha(tokenizer) != ENGINE["tokenizer_sha"]:
        raise ValueError("pinned official tokenizer file changed")
    if runtime.get("identity", {}).get("tokenizer_sha256") != ENGINE["tokenizer_sha"]:
        raise ValueError("official counter tokenizer identity changed")
    probe = ENGINE["OfficialCounter"](runtime).invoke(
        [
            {
                "model": "deepseek-flash",
                "messages": [{"role": "user", "content": "TraceFix context cache probe"}],
                "tools": [],
                "thinking": {"type": "disabled"},
            }
        ]
    )
    if probe.get("identity") != runtime.get("identity"):
        raise ValueError("official tokenizer probe identity mismatch")
    return materials, {
        "counter_probe_sha256": digest(probe),
        "old_pilot": old_pilot_baseline(previous),
    }


def provider_setup(root: Path) -> tuple[dict, dict, dict]:
    from tracefix.runtime import load_environment_file

    if not ENV_FILE.is_file():
        raise ValueError("DeepSeek environment file unavailable")
    load_environment_file(ENV_FILE)
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise ValueError("DeepSeek API key unavailable")
    catalog = api_json(MODEL_URL, key)
    model = next((m for m in catalog.get("data", []) if m.get("id") == "deepseek-flash"), None)
    if model is None:
        raise ValueError("deepseek-flash missing from authenticated provider catalog")
    balance = wallet_snapshot(root, 0)
    if not balance.get("is_available") or balance["available_cny"] <= 0:
        raise ValueError("provider CNY wallet unavailable or empty")
    provider = {
        "catalog": catalog,
        "model_row": model,
        "transport": ENGINE["transport_identity"](),
        "model_version": "DeepSeek-V4.1-Flash",
        "context_tokens": 1_048_576,
        "max_output_tokens": 393_216,
        "thinking": "disabled",
        "temperature": 0,
        "stream": False,
        "automatic_retries": 0,
    }
    prices = {
        "source": PRICE_URL,
        "observed_at": now(),
        "model_alias": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "context_tokens": 1_048_576,
        "max_output_tokens": 393_216,
        "peak_cny_per_million": {"input_cache_miss": 2, "input_cache_hit": 0.04, "output": 8},
        "off_peak_cny_per_million": {"input_cache_miss": 1, "input_cache_hit": 0.02, "output": 4},
        "reservation_rates": {"input": 2, "output": 8},
        "note": "Official price page verified 2026-10-10; reserve assumes all input is uncached.",
    }
    return provider, prices, balance


def prepare(root: Path) -> dict:
    root = root.resolve()
    if root != OUTPUT.resolve():
        raise ValueError(f"experiment output must be {OUTPUT}")
    if root.exists() and any(root.iterdir()):
        raise ValueError("experiment directory is not empty; refusing to overwrite evidence")
    root.mkdir(parents=True, exist_ok=True)
    from tracefix.comparison_campaign import git
    from tracefix.comparison_profiles import profile_config
    from tracefix.models.input_bounds import DEEPSEEK_FLASH_CONTEXT

    if not (PARENT / "protocol.json").is_file() or not (ANCHOR / "task-selection.json").is_file():
        raise ValueError("frozen A/C task materials missing")
    previous = read_json(PARENT / "protocol.json")
    if digest(previous) != read_json(PARENT / "protocol.sha256.json")["sha256"]:
        raise ValueError("previous campaign protocol hash mismatch")
    if previous.get("profile", {}).get("name") != PROFILE_NAME:
        raise ValueError("expected the frozen ac-30-180s-v1 campaign")
    selection = read_json(ANCHOR / "task-selection.json")
    task_ids = selection.get("selected_task_ids", [])
    if len(task_ids) != 30 or set(task_ids) != set(previous["tasks"]):
        raise ValueError("frozen 30-task selection changed")
    materials, calibration = verify_inputs(previous, task_ids)
    provider, prices, balance = provider_setup(root)
    profile = profile_config(PROFILE_NAME)
    if profile["limits"]["timeout_seconds"] != REQUEST_SECONDS or not profile["capability_mode"]:
        raise ValueError("C execution profile does not match the approved run")

    prompts = {}
    for task_id in task_ids:
        source = Path(previous["tasks"][task_id]["prompt_path"])
        target = root / "materials" / "prompts" / f"{task_id}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        prompts[task_id] = {"path": str(target), "sha256": file_sha(target)}
    task_order = [PILOT_TASK, *(task for task in task_ids if task != PILOT_TASK)]
    schedule = [
        {"id": f"{i:03d}", "task_id": task, "repetition": 1, "arm": "C", "block": i}
        for i, task in enumerate(task_order, 1)
    ]
    tasks = {}
    for task in task_order:
        spec = json.loads(json.dumps(previous["tasks"][task]))
        spec["prompt_path"] = prompts[task]["path"]
        spec["prompt_sha256"] = prompts[task]["sha256"]
        tasks[task] = spec

    implementation = ENGINE["TraceFixRunner"]._implementation_sha256()
    manifest = source_manifest()
    archive_sha = save_source_snapshot(root)
    auth_path = root / "authorization.json"
    auth = {
        "kind": "tracefix_paid_authorization",
        "profile": PROFILE_NAME,
        "limit_cny": TOTAL_CNY,
        "authorization_id": "20261010-c-context-30-" + uuid.uuid4().hex,
        "authorization_path": str(auth_path),
        "user_instruction": (
            "User authorized one paid C retest, then the same C thirty-task run if the pilot gate "
            "passes; one independent ¥300 limit."
        ),
        "created_at": now(),
    }
    write_json(auth_path, auth)
    claim = {"campaign": str(root), "authorization_sha256": file_sha(auth_path)}
    write_json(auth_path.with_suffix(".claim.json"), claim)
    funding = {
        "kind": "independent_authorization",
        "authorization_cny": TOTAL_CNY,
        "prior_conservative_cny": 0.0,
        "authorization": str(auth_path),
        "authorization_sha256": file_sha(auth_path),
        "authorization_id": auth["authorization_id"],
        "campaign": str(root),
        "claim_sha256": digest(claim),
    }
    profile["limits"]["active_seconds"] = TASK_SECONDS
    profile["limits"]["timeout_seconds"] = REQUEST_SECONDS
    profile["limits"]["limit_cny"] = TOTAL_CNY
    profile["trial_limit_cny"] = TASK_CNY
    protocol = {
        "schema_version": 1,
        "experiment": "post-context-fix C-only retest of the original thirty tasks",
        "mode": "live",
        "profile": profile,
        "implementation_commit": git(REPO, "rev-parse", "HEAD"),
        "implementation_sha256": implementation,
        "implementation_files": manifest,
        "source_snapshot_path": str((root / "source-snapshot.zip").resolve()),
        "source_snapshot_sha256": archive_sha,
        "driver_path": str(Path(__file__).resolve()),
        "driver_sha256": file_sha(Path(__file__).resolve()),
        "parent_campaign": str(PARENT),
        "parent_protocol_sha256": file_sha(PARENT / "protocol.json"),
        "parent_summary_sha256": file_sha(PARENT / "summary.json"),
        "task_selection_sha256": file_sha(ANCHOR / "task-selection.json"),
        "counter_runtime": previous["counter_runtime"],
        "counter_probe_sha256": calibration["counter_probe_sha256"],
        "tokenizer_sha256": ENGINE["tokenizer_sha"],
        "provider_transport": provider["transport"],
        "provider": provider,
        "prices": prices,
        "funding": funding,
        "authorization_cny": TOTAL_CNY,
        "task_max_cny": TASK_CNY,
        "active_seconds": TASK_SECONDS,
        "request_timeout_seconds": REQUEST_SECONDS,
        "request_retries": 0,
        "old_pilot_baseline": calibration["old_pilot"],
        "improvement_gate": {
            "overall_hit_rate_minimum": EXPECTED_OLD_OVERALL + OVERALL_IMPROVEMENT,
            "overall_percentage_point_gain_minimum": 10.0,
            "fact_hit_rate_minimum": FACT_HIT_FLOOR,
            "fact_request_count_minimum": MIN_FACT_REQUESTS,
            "prefix_change_allowed_only_at_context_compaction": True,
        },
        "task_order": task_order,
        "schedule": schedule,
        "tasks": tasks,
        "qualification_reused": True,
        "qualification_note": (
            "Reuses previous task qualification and frozen materials; no offline repair tests or "
            "CI were run."
        ),
        "frozen_at": now(),
    }
    write_json(root / "prices.json", prices)
    write_json(root / "provider-models.json", provider["catalog"])
    write_json(root / "source-manifest.json", manifest)
    write_json(root / "protocol.json", protocol)
    write_json(root / "protocol.sha256.json", {"sha256": digest(protocol)})
    write_json(
        root / "storage-owner.json",
        {
            "owner": "TraceFix 2026-10-10 post-context-fix C retest",
            "campaign": str(root),
            "contains": (
                "Provider requests/responses, wallet snapshots, workspaces, patches, test and "
                "verification evidence."
            ),
            "cleanup_after_review": True,
            "cleanup_command": f"Remove-Item -LiteralPath '{root}' -Recurse -Force",
            "created_at": now(),
        },
    )
    write_json(
        root / "progress.json",
        {
            "started": 0,
            "planned": 30,
            "known_peak_cny": 0.0,
            "unknown_reserved_cny": 0.0,
            "gate": "pilot_pending",
            "initial_available_cny": balance["available_cny"],
        },
    )
    write_json(
        root / "startup-gate.json",
        {
            "accepted": True,
            "paid_calls_before_gate": 0,
            "available_cny": balance["available_cny"],
            "model_alias": "deepseek-flash",
            "model_version": provider["model_version"],
            "context_tokens": DEEPSEEK_FLASH_CONTEXT,
            "provider_models_sha256": file_sha(root / "provider-models.json"),
            "tokenizer_probe_sha256": calibration["counter_probe_sha256"],
            "old_pilot_baseline": calibration["old_pilot"],
            "created_at": now(),
        },
    )
    return protocol


def check_frozen(root: Path) -> dict:
    protocol = read_json(root / "protocol.json")
    if digest(protocol) != read_json(root / "protocol.sha256.json")["sha256"]:
        raise ValueError("campaign protocol hash changed")
    if file_sha(Path(protocol["driver_path"])) != protocol["driver_sha256"]:
        raise ValueError("campaign driver changed after freeze")
    if ENGINE["TraceFixRunner"]._implementation_sha256() != protocol["implementation_sha256"]:
        raise ValueError("TraceFix source changed after experiment freeze")
    if file_sha(Path(protocol["source_snapshot_path"])) != protocol["source_snapshot_sha256"]:
        raise ValueError("source snapshot archive changed")
    return protocol


def prefix_audit(root: Path, row: dict, record: dict, requests: list[dict]) -> dict:
    segment = root / "segments" / row["id"]
    trial = segment / "trials" / row["id"]
    ordered = sorted(
        (r for r in requests if r["trial_id"] == row["id"] and r["status"] == "completed"),
        key=lambda r: r["id"],
    )
    bodies = []
    for request in ordered:
        path = segment / "provider" / f"{request['id']}-request.json"
        if file_sha(path) != request["request_sha256"]:
            raise ValueError("request evidence hash changed")
        bodies.append(read_json(path))
    trajectory = Path(record["agent_result"]).with_name("trajectory.jsonl")
    if not trajectory.is_file():
        raise ValueError("agent trace missing for prefix audit")
    events = [json.loads(line) for line in trajectory.read_text(encoding="utf-8").splitlines()]
    model_events = [
        i for i, event in enumerate(events) if event.get("event_type") == "model_requested"
    ]
    if len(model_events) < len(bodies):
        raise ValueError("model request trace is incomplete")
    compact_events = {
        i
        for i, event in enumerate(events)
        if event.get("event_type") == "context_compacted"
        and (
            event.get("payload", {}).get("compacted") is True
            or int(event.get("payload", {}).get("tool_results_pruned", 0)) > 0
        )
    }
    facts, transitions, anomalies = [], [], []
    for index, body in enumerate(bodies):
        messages, tools = body.get("messages", []), body.get("tools", [])
        if index == 0:
            transitions.append({"request": ordered[index]["id"], "kind": "first"})
            continue
        previous = bodies[index - 1]
        old_messages = previous.get("messages", [])
        prefix_ok = (
            len(messages) >= len(old_messages) and messages[: len(old_messages)] == old_messages
        )
        tools_same = tools == previous.get("tools", [])
        suffix = messages[len(old_messages) :] if prefix_ok else []
        fact = any(
            isinstance(message.get("content"), str)
            and "[TraceFix 已核验任务事实]" in message["content"]
            for message in suffix
        )
        if fact:
            facts.append(ordered[index]["id"])
        left, right = model_events[index - 1], model_events[index]
        compacted = any(left < event_index < right for event_index in compact_events)
        if prefix_ok and tools_same:
            kind = "append_fact" if fact else "append_only"
        elif compacted and tools_same:
            kind = "context_compaction_boundary"
        else:
            kind = "unexpected_prefix_or_tool_change"
            anomalies.append(
                {"request": ordered[index]["id"], "prefix_ok": prefix_ok, "tools_same": tools_same}
            )
        transitions.append({"request": ordered[index]["id"], "kind": kind})
    hits = sum(
        int(r.get("supplier_raw_usage", {}).get("prompt_cache_hit_tokens", 0)) for r in ordered
    )
    inputs = sum(int(r.get("usage", {}).get("input_tokens", 0)) for r in ordered)
    fact_ids = set(facts)
    fact_rows = [r for r in ordered if r["id"] in fact_ids]
    fact_hits = sum(int(r["supplier_raw_usage"]["prompt_cache_hit_tokens"]) for r in fact_rows)
    fact_inputs = sum(int(r["usage"]["input_tokens"]) for r in fact_rows)
    result = {
        "trial_id": row["id"],
        "task_id": row["task_id"],
        "request_count": len(ordered),
        "input_tokens": inputs,
        "cache_hit_tokens": hits,
        "overall_hit_rate": hits / inputs if inputs else None,
        "fact_request_ids": facts,
        "fact_input_tokens": fact_inputs,
        "fact_hit_tokens": fact_hits,
        "fact_hit_rate": fact_hits / fact_inputs if fact_inputs else None,
        "compression_boundaries": sum(
            t["kind"] == "context_compaction_boundary" for t in transitions
        ),
        "prefix_anomalies": anomalies,
        "transitions": transitions,
        "trace_sha256": file_sha(trajectory),
    }
    write_json(trial / "context-prefix-audit.json", result)
    return result


def evaluate_pilot(root: Path, protocol: dict) -> dict:
    row = protocol["schedule"][0]
    segment = root / "segments" / row["id"]
    record_path = segment / "trials" / row["id"] / "record.json"
    if not record_path.is_file():
        return {"passed": False, "reason": "pilot record not finished"}
    record = read_json(record_path)
    local = read_json(segment / "protocol.json")
    requests = ENGINE["validate_requests"](segment, local)
    if any(r.get("status") != "completed" or r.get("official_count_delta") != 0 for r in requests):
        result = {"passed": False, "reason": "unknown request or tokenizer usage mismatch"}
        write_json(root / "pilot-gate.json", result)
        return result
    audit = prefix_audit(root, row, record, requests)
    old = protocol["old_pilot_baseline"]
    overall = audit["overall_hit_rate"] or 0.0
    fact_rate = audit["fact_hit_rate"]
    conditions = {
        "overall_at_least_34_57_percent": overall
        >= protocol["improvement_gate"]["overall_hit_rate_minimum"],
        "two_fact_requests_at_least_90_percent": len(audit["fact_request_ids"]) >= MIN_FACT_REQUESTS
        and fact_rate is not None
        and fact_rate >= FACT_HIT_FLOOR,
        "no_unexpected_prefix_change": not audit["prefix_anomalies"],
        "complete_usage_evidence": bool(requests)
        and all(r.get("status") == "completed" for r in requests),
    }
    result = {
        "passed": all(conditions.values()),
        "conditions": conditions,
        "old_overall_hit_rate": old["overall_hit_rate"],
        "new_overall_hit_rate": overall,
        "improvement_percentage_points": (overall - old["overall_hit_rate"]) * 100,
        "old_fact_hit_rate": old["fact_hit_rate"],
        "new_fact_hit_rate": fact_rate,
        "fact_request_count": len(audit["fact_request_ids"]),
        "independent_patch_passed": bool(record.get("passed")),
        "normal_completion": record.get("status") == "completed",
        "input_tokens": audit["input_tokens"],
        "cache_hit_tokens": audit["cache_hit_tokens"],
        "output_tokens": sum(int(r["usage"]["output_tokens"]) for r in requests),
        "known_peak_cny": sum(float(r["peak_cost_cny"]) for r in requests),
        "seconds": record.get("timings", {}).get("delivery_seconds", record.get("seconds")),
        "context_prefix_audit_sha256": file_sha(record_path.parent / "context-prefix-audit.json"),
    }
    write_json(root / "pilot-gate.json", result)
    return result


def finish_unknown(segment: Path, row: dict, local: dict) -> dict:
    record_path = segment / "trials" / row["id"] / "record.json"
    record = read_json(record_path)
    requests = ENGINE["validate_requests"](segment, local)
    if record.get("status") != "unknown_request" or not any(
        r.get("status") != "completed" for r in requests
    ):
        raise ValueError("unknown response is not durably recorded")
    copy_path = record_path.parent / "agent-unknown.json"
    if not copy_path.exists():
        shutil.copyfile(record_path, copy_path)
    patch = record_path.parent / "patch.diff"
    record["patch_sha256"] = file_sha(patch) if patch.exists() else None
    record["terminal_provider_unknown"] = True
    result = ENGINE["finalize_trial"](record_path.parent, record, local["tasks"][row["task_id"]])
    reason = (result.get("verification") or {}).get("reason", "")
    if str(reason).startswith("infrastructure:"):
        raise ValueError("independent verification infrastructure failure")
    return result


def records(root: Path, protocol: dict) -> list[dict]:
    result = []
    for row in protocol["schedule"]:
        path = root / "segments" / row["id"] / "trials" / row["id"] / "record.json"
        if path.is_file():
            result.append(read_json(path))
    return result


def liability(root: Path) -> tuple[float, float]:
    known = unknown = 0.0
    for path in sorted((root / "segments").glob("[0-9][0-9][0-9]/requests.json")):
        for row in read_json(path).get("requests", []):
            if "peak_cost_cny" in row:
                known += float(row["peak_cost_cny"])
            elif row.get("status") != "completed":
                unknown += float(row.get("reserved_peak_cny", 0))
    return known, unknown


def read_all_requests(root: Path) -> list[dict]:
    result = []
    for path in sorted((root / "segments").glob("[0-9][0-9][0-9]/requests.json")):
        segment = path.parent
        local = read_json(segment / "protocol.json")
        result.extend(ENGINE["validate_requests"](segment, local))
    return result


def update_progress(root: Path, protocol: dict) -> dict:
    run_records = records(root, protocol)
    known, unknown = liability(root)
    requests = read_all_requests(root)
    inputs = sum(int(r.get("usage", {}).get("input_tokens", 0)) for r in requests)
    hits = sum(
        int(r.get("supplier_raw_usage", {}).get("prompt_cache_hit_tokens", 0)) for r in requests
    )
    main = sum(r.get("status") == "completed" and bool(r.get("passed")) for r in run_records)
    data = {
        "started": len(run_records),
        "planned": 30,
        "finished": sum(bool(r.get("finished")) for r in run_records),
        "main_successes": main,
        "patch_passes": sum(bool(r.get("passed")) for r in run_records),
        "normal_completions": sum(r.get("status") == "completed" for r in run_records),
        "known_peak_cny": known,
        "unknown_reserved_cny": unknown,
        "input_tokens": inputs,
        "cache_hit_tokens": hits,
        "cache_hit_rate": hits / inputs if inputs else None,
        "updated_at": now(),
    }
    if (root / "pilot-gate.json").is_file():
        data["pilot_gate"] = read_json(root / "pilot-gate.json")
    write_json(root / "progress.json", data)
    return data


def verify_task(spec: dict, service_identity_file: Path) -> None:
    from tracefix.comparison_campaign import git
    from tracefix.provenance import inspect_test_environment

    source, python, prompt = Path(spec["source"]), Path(spec["python"]), Path(spec["prompt_path"])
    if not source.is_dir() or not python.is_file() or not prompt.is_file():
        raise ValueError("qualified source, interpreter or prompt missing")
    if file_sha(prompt) != spec["prompt_sha256"]:
        raise ValueError("frozen task prompt changed")
    if git(source, "rev-parse", "HEAD") != spec["source_identity"]["commit"]:
        raise ValueError("task source commit changed")
    if git(source, "status", "--porcelain"):
        raise ValueError("qualified source working tree changed")
    environment = inspect_test_environment(
        python, pythonpath_entries=tuple(Path(p) for p in spec.get("pythonpath", []))
    )
    if environment.fingerprint_sha256 != spec["environment_identity"]:
        raise ValueError("qualified environment identity changed")
    identity = spec.get("service_identity")
    if identity:
        if not service_identity_file.is_file():
            raise ValueError("qualified controlled-service restart receipt missing")
        current = read_json(service_identity_file)
        mutable = {"pid", "started_at", "startup_nonce", "identity_url"}
        if any(identity.get(k) != current.get(k) for k in identity if k not in mutable):
            raise ValueError("controlled service build or environment changed")
        if current.get("port") != identity.get("port") or current.get("health_url") != identity.get(
            "health_url"
        ):
            raise ValueError("controlled service endpoint changed")
        req = Request(current["identity_url"])
        with build_opener(ProxyHandler({})).open(req, timeout=8) as response:
            actual = json.loads(response.read())
        if actual != current:
            raise ValueError("controlled service identity endpoint mismatch")
        if any(actual.get(k) != identity.get(k) for k in identity if k not in mutable):
            raise ValueError("controlled service identity changed")
        with build_opener(ProxyHandler({})).open(current["health_url"], timeout=8) as response:
            health = json.loads(response.read())
        if health.get("url") != current["health_url"]:
            raise ValueError("controlled service health check failed")


def run(root: Path, max_trials: int = 30) -> list[dict]:
    root = root.resolve()
    with ENGINE["ProcessLock"](root):
        protocol = check_frozen(root)
        from tracefix.runtime import load_environment_file

        load_environment_file(ENV_FILE)
        if (root / "halt.json").exists():
            raise ValueError("campaign halted; no automatic replay")
        pilot = protocol["schedule"][0]
        gate_path = root / "pilot-gate.json"
        gate = read_json(gate_path) if gate_path.is_file() else None
        pilot_record = root / "segments" / pilot["id"] / "trials" / pilot["id"] / "record.json"
        if gate is None and pilot_record.is_file() and read_json(pilot_record).get("finished"):
            gate = evaluate_pilot(root, protocol)
        if gate is not None and not gate.get("passed"):
            update_progress(root, protocol)
            return records(root, protocol)

        result_rows = []
        previous_segment = None
        for row in protocol["schedule"][:max_trials]:
            segment = root / "segments" / row["id"]
            record_path = segment / "trials" / row["id"] / "record.json"
            local_path = segment / "protocol.json"
            if record_path.exists():
                existing = read_json(record_path)
                local = read_json(local_path)
                if not existing.get("finished"):
                    if existing.get("phase") == "agent_completed":
                        existing = ENGINE["finalize_trial"](
                            record_path.parent, existing, local["tasks"][row["task_id"]]
                        )
                    elif existing.get("status") == "unknown_request":
                        existing = finish_unknown(segment, row, local)
                    else:
                        raise ValueError("incomplete non-provider run; refusing replay")
                ENGINE["validate_record"](existing, record_path.parent, row)
                result_rows.append(existing)
                previous_segment = (
                    segment if (segment / "requests.json").is_file() else previous_segment
                )
                if row["id"] == pilot["id"] and gate is None:
                    gate = evaluate_pilot(root, protocol)
                    if not gate["passed"]:
                        break
                continue
            if row["id"] != pilot["id"] and (gate is None or not gate.get("passed")):
                break
            check_frozen(root)
            spec = protocol["tasks"][row["task_id"]]
            verify_task(spec, root / "httpbin-identity.json")
            known_before, unknown_before = liability(root)
            if known_before + unknown_before >= TOTAL_CNY:
                write_json(
                    root / "budget-stop.json",
                    {"sent": False, "reason": "authorization liability exhausted"},
                )
                break
            balance = wallet_snapshot(root, row["block"])
            wallet_cap = min(
                TASK_CNY,
                TOTAL_CNY - known_before - unknown_before,
                balance["available_cny"] - unknown_before,
            )
            if wallet_cap <= 0:
                write_json(
                    root / "wallet-stop.json",
                    {
                        "sent": False,
                        "balance_cny": balance["available_cny"],
                        "unknown_reserve_cny": unknown_before,
                    },
                )
                break
            segment.mkdir(parents=True, exist_ok=False)
            if previous_segment is None:
                local_funding = protocol["funding"]
            else:
                known, unknown = liability(root)
                local_funding = {
                    "kind": "b_chain",
                    "parent_campaign": str(previous_segment.resolve()),
                    "requests_sha256": file_sha(previous_segment / "requests.json"),
                    "parent_protocol_sha256": file_sha(previous_segment / "protocol.json"),
                    "prior_conservative_cny": known + unknown,
                    "known_cny": known,
                    "unknown_reserve_cny": unknown,
                    "authorization_cny": TOTAL_CNY,
                }
            local = {
                **protocol,
                "tasks": {row["task_id"]: spec},
                "schedule": [row],
                "funding": local_funding,
                "wallet_cap_cny": wallet_cap,
                "wallet_snapshot_sha256": file_sha(root / "wallet" / f"{row['block']:02d}.json"),
            }
            write_json(local_path, local)
            write_json(segment / "protocol.sha256.json", {"sha256": digest(local)})
            write_json(
                segment / "requests.json",
                {
                    "schema_version": 1,
                    "protocol_sha256": digest(local),
                    "limit_cny": TOTAL_CNY,
                    "requests": [],
                },
            )
            try:
                result = ENGINE["campaign"].execute_trial(segment, local, row, ENV_FILE)
            except ValueError as exc:
                if str(exc) != "unknown provider result; campaign halted":
                    raise
                result = finish_unknown(segment, row, local)
            request_rows = ENGINE["validate_requests"](segment, local)
            if any(
                r.get("official_count_delta") != 0
                for r in request_rows
                if r.get("status") == "completed"
            ):
                write_json(
                    root / "halt.json",
                    {
                        "reason": "official tokenizer count differs from supplier usage",
                        "trial_id": row["id"],
                    },
                )
                raise ValueError("tokenizer count discrepancy; campaign halted")
            ledger = read_json(segment / "requests.json")
            if ledger.get("halt_reason"):
                write_json(
                    root / "halt.json", {"reason": ledger["halt_reason"], "trial_id": row["id"]}
                )
                raise ValueError("provider identity or usage anomaly; campaign halted")
            ENGINE["validate_record"](result, record_path.parent, row)
            if result.get("infrastructure_failure") and not result.get("terminal_provider_unknown"):
                write_json(
                    root / "halt.json",
                    {
                        "reason": "independent verification infrastructure failure",
                        "trial_id": row["id"],
                    },
                )
                raise ValueError("verification infrastructure failure; campaign halted")
            all_requests = read_all_requests(root)
            fingerprints = {
                str(r["supplier_system_fingerprint"])
                for r in all_requests
                if r.get("status") == "completed" and r.get("supplier_system_fingerprint")
            }
            if len(fingerprints) > 1:
                write_json(
                    root / "halt.json",
                    {
                        "reason": "supplier system fingerprint changed",
                        "fingerprints": sorted(fingerprints),
                        "trial_id": row["id"],
                    },
                )
                raise ValueError("provider fingerprint changed; campaign halted")
            trial_requests = [r for r in all_requests if r.get("trial_id") == row["id"]]
            audit = prefix_audit(root, row, result, trial_requests)
            if audit["prefix_anomalies"]:
                write_json(
                    root / "halt.json",
                    {
                        "reason": "request prefix changed outside compaction",
                        "trial_id": row["id"],
                        "anomalies": audit["prefix_anomalies"],
                    },
                )
                raise ValueError("unexpected prefix change; campaign halted")
            result_rows.append(result)
            previous_segment = segment
            progress = update_progress(root, protocol)
            print(
                json.dumps(
                    {
                        "completed": progress["finished"],
                        "planned": 30,
                        "task": row["task_id"],
                        "status": result.get("status"),
                        "main_success": result.get("status") == "completed"
                        and bool(result.get("passed")),
                        "patch_pass": bool(result.get("passed")),
                        "cache_hit_rate": progress["cache_hit_rate"],
                        "known_peak_cny": progress["known_peak_cny"],
                        "unknown_reserved_cny": progress["unknown_reserved_cny"],
                        "fact_hit_rate": audit["fact_hit_rate"],
                        "fact_requests": len(audit["fact_request_ids"]),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if row["id"] == pilot["id"]:
                gate = evaluate_pilot(root, protocol)
                update_progress(root, protocol)
                print(
                    json.dumps(
                        {"pilot_gate": "passed" if gate["passed"] else "not_met", **gate},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if not gate["passed"]:
                    break
        return result_rows


def report(root: Path) -> dict:
    protocol = check_frozen(root.resolve())
    from tracefix.comparison_holdout import main_success

    run_records = records(root, protocol)
    requests = read_all_requests(root)
    per_trial = {}
    for request in requests:
        per_trial.setdefault(request["trial_id"], []).append(request)
    inputs = sum(int(r.get("usage", {}).get("input_tokens", 0)) for r in requests)
    outputs = sum(int(r.get("usage", {}).get("output_tokens", 0)) for r in requests)
    hits = sum(
        int(r.get("supplier_raw_usage", {}).get("prompt_cache_hit_tokens", 0)) for r in requests
    )
    misses = sum(
        int(r.get("supplier_raw_usage", {}).get("prompt_cache_miss_tokens", 0)) for r in requests
    )
    known, unknown = liability(root)
    summary = {
        "protocol_sha256": digest(protocol),
        "planned": len(protocol["schedule"]),
        "started": len(run_records),
        "finished": sum(bool(r.get("finished")) for r in run_records),
        "main_successes": sum(bool(main_success(r)) for r in run_records),
        "independent_patch_passes": sum(bool(r.get("passed")) for r in run_records),
        "normal_completions": sum(r.get("status") == "completed" for r in run_records),
        "input_tokens": inputs,
        "output_tokens": outputs,
        "cache_hit_tokens": hits,
        "cache_miss_tokens": misses,
        "cache_hit_rate": hits / inputs if inputs else None,
        "known_peak_cny": known,
        "unknown_reserved_cny": unknown,
        "total_conservative_liability_cny": known + unknown,
        "cache_adjusted_peak_estimate_cny": (hits * 0.04 + misses * 2 + outputs * 8) / 1_000_000,
        "all_started_cost_per_main_success_cny": (known + unknown)
        / sum(bool(main_success(r)) for r in run_records)
        if any(main_success(r) for r in run_records)
        else None,
        "seconds": sum(
            float(r.get("timings", {}).get("delivery_seconds", r.get("seconds", 0)))
            for r in run_records
        ),
        "pilot_gate": read_json(root / "pilot-gate.json")
        if (root / "pilot-gate.json").is_file()
        else None,
        "unknown_requests": [r["id"] for r in requests if r.get("status") != "completed"],
        "interpretation": (
            "Post-context-fix C-only historical comparison; nonconcurrent exploratory run. "
            "Cache-adjusted estimate uses published peak prices and is not a provider invoice."
        ),
    }
    old = {}
    with OLD_CSV.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            old[(row["task_id"], row["arm"])] = row
    table = []
    for schedule_row in protocol["schedule"]:
        record = next((r for r in run_records if r["id"] == schedule_row["id"]), None)
        old_c = old.get((schedule_row["task_id"], "C"), {})
        old_b = old.get((schedule_row["task_id"], "B"), {})
        sent = per_trial.get(schedule_row["id"], [])
        row = {
            "task_id": schedule_row["task_id"],
            "new_c_status": record.get("status") if record else "unstarted",
            "new_c_main_success": main_success(record) if record else False,
            "new_c_patch_pass": record.get("passed", False) if record else False,
            "new_c_seconds": record.get("timings", {}).get(
                "delivery_seconds", record.get("seconds")
            )
            if record
            else None,
            "new_c_requests": len(sent),
            "new_c_input_tokens": sum(int(x.get("usage", {}).get("input_tokens", 0)) for x in sent),
            "new_c_cache_hit_tokens": sum(
                int(x.get("supplier_raw_usage", {}).get("prompt_cache_hit_tokens", 0)) for x in sent
            ),
            "old_c_main_success": old_c.get("main_success"),
            "old_c_seconds": old_c.get("seconds"),
            "old_c_cache_hit_rate": (
                float(old_c["cache_hit_tokens"]) / float(old_c["input_tokens"])
            )
            if old_c.get("cache_hit_tokens") and old_c.get("input_tokens")
            else None,
            "b_main_success": old_b.get("main_success"),
            "b_seconds": old_b.get("seconds"),
            "b_cache_hit_rate": (float(old_b["cache_hit_tokens"]) / float(old_b["input_tokens"]))
            if old_b.get("cache_hit_tokens") and old_b.get("input_tokens")
            else None,
        }
        table.append(row)
    write_json(root / "summary.json", summary)
    if table:
        with (root / "per-task.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(table[0]))
            writer.writeheader()
            writer.writerows(table)
    lines = [
        "# Post-context-fix C cache retest",
        "",
        (
            f"Started {summary['started']}/{summary['planned']}; "
            f"main success {summary['main_successes']}; "
            f"patch pass {summary['independent_patch_passes']}; "
            f"normal completion {summary['normal_completions']}."
        ),
        f"Cache hit: {hits}/{inputs} = {summary['cache_hit_rate']:.2%}.",
        (
            f"Known conservative peak cost ¥{known:.6f}; unknown reserved ¥{unknown:.6f}; "
            f"cache-adjusted peak estimate ¥{summary['cache_adjusted_peak_estimate_cny']:.6f}."
        ),
        f"Time {summary['seconds']:.2f}s. {summary['interpretation']}",
        "",
        "| Task | New C status | New C success | Old C success | B success | New C hit/input |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in table:
        hit = (
            f"{row['new_c_cache_hit_tokens']}/{row['new_c_input_tokens']}"
            if row["new_c_input_tokens"]
            else "—"
        )
        lines.append(
            f"| {row['task_id']} | {row['new_c_status']} | {row['new_c_main_success']} | "
            f"{row['old_c_main_success']} | {row['b_main_success']} | {hit} |"
        )
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_json(
        root / "evidence-index.json",
        {
            str(p.relative_to(root)): file_sha(p)
            for p in root.rglob("*")
            if p.is_file() and p.name not in {"evidence-index.json", ".tracefix.lock"}
        },
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "report"))
    parser.add_argument("--campaign-dir", type=Path, default=OUTPUT)
    parser.add_argument("--max-trials", type=int, default=30)
    args = parser.parse_args(argv)
    if args.action == "prepare":
        prepare(args.campaign_dir)
    elif args.action == "run":
        run(args.campaign_dir, max_trials=args.max_trials)
    else:
        print(json.dumps(report(args.campaign_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
