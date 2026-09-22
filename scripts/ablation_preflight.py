"""Freeze offline ablation evidence; optional 120-trial simulation, never paid calls."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

from tracefix.ablation import budget_preflight, summarize_ablation
from tracefix.holdout import freeze_holdout
from tracefix.p2_protocol import P2ProtocolConfig, run_p2_simulation, write_p2_summary

DEFAULT_P1_EVIDENCE = Path(
    "runs/p1-revalidation-20260917/behavior-validation/behavior-validation.json"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)


def validate_recipe_lock(task_id: str, row: dict, entry: dict, recipe: Path) -> None:
    """Do not attach a newly edited recipe to an old successful qualification."""
    actual = {
        "task_id": task_id,
        "base_commit": row["base_commit"],
        "recipe_sha256": sha256(recipe),
        "dependency_fingerprint": row["environment_before"]["fingerprint_sha256"],
        "target_node_ids": row["initial_evidence"]["expected_node_ids"],
    }
    if any(entry.get(key) != value for key, value in actual.items()):
        raise RuntimeError(f"frozen recipe identity changed: {task_id}")
    if row["gold_evidence"]["expected_node_ids"] != actual["target_node_ids"]:
        raise RuntimeError(f"gold targets changed: {task_id}")
    environment = row["environment_before"]
    payload = {
        key: environment.get(key, {})
        for key in ("python_version", "dependency_versions", "pythonpath_artifacts")
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if fingerprint != actual["dependency_fingerprint"]:
        raise RuntimeError(f"dependency inventory does not match its fingerprint: {task_id}")


def validate_pricing(path: Path) -> dict:
    pricing = json.loads(path.read_text(encoding="utf-8"))
    if pricing.get("currency") != "CNY" or pricing.get("model") != "deepseek-flash":
        raise RuntimeError("pricing currency/model mismatch")
    if sha256(Path(pricing["local_html"])) != pricing["html_sha256"]:
        raise RuntimeError("pricing HTML hash mismatch")
    return pricing


def inspect_service_process(pid_file: Path, port: int) -> dict:
    """Record observable process evidence; health alone never establishes identity."""
    observation = {"identity_status": "unknown", "pid_file": str(pid_file)}
    try:
        process_id = int(pid_file.read_text(encoding="utf-8").strip())
        observation["recorded_pid"] = process_id
        if os.name != "nt":
            observation["reason"] = "Windows process inspection unavailable"
            return observation
        command = (
            "$ErrorActionPreference='Stop'; "
            f"$serviceProcess=Get-CimInstance Win32_Process -Filter 'ProcessId={process_id}'; "
            f"$listeners=@(Get-NetTCPConnection -State Listen -LocalPort {port}); "
            "$owners=@($listeners | ForEach-Object { Get-CimInstance Win32_Process "
            "-Filter ('ProcessId=' + $_.OwningProcess) }); "
            "@{process=$serviceProcess | Select-Object ProcessId,ParentProcessId,"
            "ExecutablePath,CommandLine,CreationDate; listeners=$listeners | "
            "Select-Object LocalAddress,LocalPort,OwningProcess; owners=$owners | "
            "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine,CreationDate} "
            "| ConvertTo-Json -Depth 4"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode:
            observation["reason"] = result.stderr.strip()
        else:
            payload = json.loads(result.stdout)
            observation["process_observation"] = payload
            environment = pid_file.parent.resolve()
            cfg = dict(
                line.split(" = ", 1)
                for line in (environment / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
                if " = " in line
            )
            verified = verify_process_chain(payload, environment, Path(cfg["home"]), port)
            observation["identity_status"] = "verified_process_chain" if verified else "unknown"
            observation["reason"] = (
                "Verified loopback listener, launcher ancestry, executable and Flask arguments; "
                "installed dependency inventory is current disk state, not a startup snapshot"
                if verified
                else "Listener does not match expected virtual-environment launcher"
            )
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        observation["reason"] = str(exc)
    return observation


def verify_process_chain(payload: dict, environment: Path, base_home: Path, port: int) -> bool:
    process = payload.get("process") or {}
    listeners = payload.get("listeners") or []
    owners = payload.get("owners") or []
    listeners = [listeners] if isinstance(listeners, dict) else listeners
    owners = [owners] if isinstance(owners, dict) else owners
    suffix = f" -m flask --app httpbin:app run --host 127.0.0.1 --port {port} --no-reload"

    def matches(row: dict, executable: Path) -> bool:
        command = row.get("CommandLine") or ""
        return Path(
            row.get("ExecutablePath") or ""
        ).resolve() == executable.resolve() and command in (
            str(executable) + suffix,
            f'"{executable}"' + suffix,
        )

    if not matches(process, environment / "Scripts" / "python.exe"):
        return False
    if len(listeners) != 1 or len(owners) != 1:
        return False
    listener, owner = listeners[0], owners[0]
    return (
        listener.get("LocalAddress") == "127.0.0.1"
        and listener.get("LocalPort") == port
        and listener.get("OwningProcess") == owner.get("ProcessId")
        and (
            owner.get("ProcessId") == process.get("ProcessId")
            or (
                owner.get("ParentProcessId") == process.get("ProcessId")
                and matches(owner, base_home / "python.exe")
            )
        )
    )


def prepare_output(
    output: Path,
    *,
    resume: bool,
    pricing_evidence: Path,
    p1_evidence: Path = DEFAULT_P1_EVIDENCE,
) -> bool:
    """An existing directory is immutable; explicit resume validates all frozen inputs."""
    if not resume:
        output.mkdir(parents=True, exist_ok=False)
        return False
    manifest = json.loads((output / "preflight-manifest.json").read_text(encoding="utf-8"))
    if str(pricing_evidence.resolve()) != manifest["pricing_evidence"]:
        raise RuntimeError("resume pricing evidence path changed")
    if manifest.get("p1_evidence", str(p1_evidence.resolve())) != str(p1_evidence.resolve()):
        raise RuntimeError("resume P1 evidence path changed")
    for group in ("inputs", "outputs"):
        for raw, expected in manifest[group].items():
            if sha256(Path(raw)) != expected:
                raise RuntimeError(f"resume frozen {group} changed: {raw}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--pricing-evidence", type=Path, required=True)
    parser.add_argument("--p1-evidence", type=Path, default=DEFAULT_P1_EVIDENCE)
    args = parser.parse_args()
    resumed_preflight = prepare_output(
        args.output,
        resume=args.resume,
        pricing_evidence=args.pricing_evidence,
        p1_evidence=args.p1_evidence,
    )
    if not resumed_preflight:
        freeze_preflight(args.output, args.pricing_evidence, args.p1_evidence)
    if args.execute:
        execute_simulation(args.output, args.p1_evidence)


def freeze_preflight(
    output: Path, pricing_evidence: Path, p1_evidence: Path = DEFAULT_P1_EVIDENCE
) -> None:
    pricing = validate_pricing(pricing_evidence)
    recipe_lock_path = Path("benchmarks/experiments/v0.8.9-holdout/recipe-lock.json")
    recipe_lock = json.loads(recipe_lock_path.read_text(encoding="utf-8"))
    old_entries = {entry["task_id"]: entry for entry in recipe_lock["entries"]}
    freeze_path = Path("benchmarks/experiments/v0.8.9-holdout/holdout-freeze.json")
    if recipe_lock["holdout_freeze_sha256"] != sha256(freeze_path):
        raise RuntimeError("original recipe lock / holdout freeze binding changed")
    report = Path("runs/holdout-final-freeze-20260922/behavior-validation-merged.json")
    frozen = freeze_holdout(
        Path("benchmarks/holdout_candidates/candidate-pool.json"),
        report,
        output / "holdout-reaudit.json",
    )
    previous = json.loads(
        Path("benchmarks/experiments/v0.8.9-holdout/holdout-freeze.json").read_text(
            encoding="utf-8"
        )
    )
    if frozen["selected_task_ids"] != previous["selected_task_ids"]:
        raise RuntimeError("holdout identity changed; review affected tasks before continuing")
    for task_id in frozen["selected_task_ids"]:
        if (
            frozen["evidence_artifact_sha256"][task_id]
            != previous["evidence_artifact_sha256"][task_id]
        ):
            raise RuntimeError(f"frozen raw artifacts changed: {task_id}")
    rows = {r["task_id"]: r for r in json.loads(report.read_text(encoding="utf-8"))}
    locks = []
    for task_id in frozen["selected_task_ids"]:
        row = rows[task_id]
        environment = row["environment_before"]
        recipe = Path("benchmarks/holdout_recipes") / f"{task_id}.json"
        validate_recipe_lock(task_id, row, old_entries[task_id], recipe)
        locks.append(
            {
                "task_id": task_id,
                "python_version": environment["python_version"],
                "dependency_versions": environment["dependency_versions"],
                "pythonpath_artifacts": environment.get("pythonpath_artifacts", {}),
                "target_node_ids": old_entries[task_id]["target_node_ids"],
                "dependency_fingerprint": environment["fingerprint_sha256"],
                "recipe_sha256": hashlib.sha256(recipe.read_bytes()).hexdigest(),
                "base_commit": row["base_commit"],
                "test_environment": {"HTTPBIN_URL": "http://127.0.0.1:8765/"}
                if task_id == "psf__requests-1921"
                else {},
            }
        )
    write_new_json(output / "dependency-lock.json", {"schema_version": 1, "tasks": locks})
    service_config = {
        "schema_version": 1,
        "task_ids": ["psf__requests-1921"],
        "package": "httpbin==0.10.2",
        "host": "127.0.0.1",
        "port": 8765,
        "python": "runs/holdout-httpbin-server-20260922/Scripts/python.exe",
        "start_argv": [
            "{python}",
            "-c",
            (
                "from httpbin import app; "
                "app.run(host='127.0.0.1', port=8765, debug=False, use_reloader=False)"
            ),
        ],
        "health_url": "http://127.0.0.1:8765/get",
        "environment": {"HTTPBIN_URL": "http://127.0.0.1:8765/"},
        "startup_policy": "Reuse healthy service; never replace an occupied port.",
    }
    service_config["dependency_versions"] = {
        d.metadata["Name"]: d.version
        for d in importlib.metadata.distributions(
            path=["runs/holdout-httpbin-server-20260922/Lib/site-packages"]
        )
    }
    service_config["dependency_inventory_scope"] = (
        "On-disk candidate environment; not attested to legacy running process"
    )
    service_config["process_identity"] = inspect_service_process(
        Path("runs/holdout-httpbin-server-20260922/server.pid"), 8765
    )
    service_config["controlled_replacement_plan"] = {
        "script": "scripts/controlled_httpbin.py",
        "port": 8766,
        "start_argv": [
            service_config["python"],
            "scripts/controlled_httpbin.py",
            "--port",
            "8766",
            "--identity-file",
            "{new_identity_file}",
        ],
        "policy": (
            "Never stop/replace unknown listeners. New port requires Requests-1921 revalidation."
        ),
        "qualification_reuse_authorized": False,
    }
    opener = build_opener(ProxyHandler({}))
    with opener.open(service_config["health_url"], timeout=5) as response:
        health = json.load(response)
    if health.get("url") != service_config["health_url"]:
        raise RuntimeError("local httpbin health response does not match")
    service_config["health_checked_at"] = datetime.now(UTC).isoformat()
    write_new_json(output / "httpbin-service.json", service_config)
    ledger_path = Path("runs/p2-formal-campaign-20260921/cost-ledger.json")
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if ledger["currency"] != "CNY" or ledger["uncertain_request"] or ledger["halt_reason"]:
        raise RuntimeError("campaign requires reconciliation")
    budget = budget_preflight(
        cap=str(ledger["cap_amount"]),
        spent=str(ledger["calculated_spent_amount"]),
        reserved=str(ledger["reserved_amount"]),
        input_price=pricing["peak_input_cache_miss_per_million"],
        output_price=pricing["peak_output_per_million"],
    )
    budget["ledger_sha256"] = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    budget["pricing_source"] = pricing["source"]
    budget["pricing_checked_at"] = pricing["retrieved_at"]
    budget["pricing_evidence_sha256"] = hashlib.sha256(pricing_evidence.read_bytes()).hexdigest()
    write_new_json(output / "budget-preflight.json", budget)
    inputs = [
        report,
        recipe_lock_path,
        freeze_path,
        pricing_evidence,
        Path(pricing["local_html"]),
        ledger_path,
        Path(__file__),
        Path("scripts/controlled_httpbin.py"),
        Path("benchmarks/holdout_candidates/candidate-pool.json"),
    ]
    inputs.extend(
        Path(old_entries[task_id]["recipe_path"]) for task_id in frozen["selected_task_ids"]
    )
    for task_id in frozen["selected_task_ids"]:
        for variant in ("initial", "gold"):
            evidence = rows[task_id][f"{variant}_evidence"]
            audit = Path(evidence["audit_path"])
            inputs.extend(
                [audit, audit.with_name("collection.audit.json"), audit.with_name("junit.xml")]
            )
            for stage in ("collection", "execution"):
                inputs.extend(
                    Path(evidence[stage][key])
                    for key in ("stdout_path", "stderr_path")
                    if evidence[stage].get(key)
                )
    inputs.extend(Path("src/tracefix").glob("**/*.py"))
    inputs.extend(Path("benchmarks/real_candidates").glob("**/*.*"))
    inputs.extend(Path("benchmarks/real_recipes").glob("**/*.json"))
    inputs.append(p1_evidence)
    outputs = [
        output / name
        for name in (
            "holdout-reaudit.json",
            "dependency-lock.json",
            "httpbin-service.json",
            "budget-preflight.json",
        )
    ]
    write_new_json(
        output / "preflight-manifest.json",
        {
            "schema_version": 1,
            "pricing_evidence": str(pricing_evidence.resolve()),
            "p1_evidence": str(p1_evidence.resolve()),
            "inputs": {str(path.resolve()): sha256(path) for path in inputs},
            "outputs": {str(path.resolve()): sha256(path) for path in outputs},
            "status": "offline_preflight_complete",
        },
    )


def validate_rehearsal_summary(summary: dict) -> None:
    """Completion alone cannot attest to a valid no-answer simulation."""
    expected = {
        "planned_count": 120,
        "completed_count": 120,
        "valid_evidence_count": 120,
        "infrastructure_count": 0,
        "verification_error_count": 0,
        "evidence_issue_count": 0,
        "success_count": 0,
    }
    if any(summary.get(key) != value for key, value in expected.items()):
        raise RuntimeError("simulation completed but independent engineering acceptance failed")


def execute_simulation(output: Path, p1_evidence: Path = DEFAULT_P1_EVIDENCE) -> None:
    if (output / "recovery-check.json").exists():
        raise RuntimeError("simulation recovery evidence already exists; never overwrite it")

    def no_provider(*args, **kwargs):
        raise AssertionError("real provider construction is forbidden in this offline rehearsal")

    config = P2ProtocolConfig(
        design="ablation",
        source_root=Path("runs/p2-formal-sources-20260920"),
        p1_evidence_path=p1_evidence,
    )
    experiment = output / "simulation"
    with patch("tracefix.p2_protocol.LiteLLMAdapter", no_provider):
        first = run_p2_simulation(config, experiment_dir=experiment)
        before = sorted(str(p) for p in (experiment / "agent-runs").iterdir())
        resumed = run_p2_simulation(config, experiment_dir=experiment)
        after = sorted(str(p) for p in (experiment / "agent-runs").iterdir())
    if resumed.resumed_count != 120 or before != after:
        raise RuntimeError("completed trials were not reused")
    write_p2_summary(experiment)
    summary = summarize_ablation(experiment)
    (output / "recovery-check.json").write_text(
        json.dumps(
            {
                "first_completed": first.completed_count,
                "resumed": resumed.resumed_count,
                "agent_directory_count_before": len(before),
                "agent_directory_count_after": len(after),
                "real_provider_constructions": 0,
                "paid_requests": 0,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    validate_rehearsal_summary(summary)


if __name__ == "__main__":
    main()
