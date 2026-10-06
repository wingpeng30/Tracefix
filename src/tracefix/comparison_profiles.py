"""Versioned evaluation resources; never alter product defaults or old protocols."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tracefix.models.input_bounds import InputBound

LEGACY_PROFILE = "abc-60k-v1"
TOKENIZER_PROFILE = "bc-350k-tokenizer"
CAPABILITY_PROFILE = "bc-capability-tokenizer"
HOLDOUT_PROFILE = "abc-holdout-capability-v1"
BC_HOLDOUT_PROFILE = "bc-holdout-180s-v1"
HOLDOUT_PROFILES = (HOLDOUT_PROFILE, BC_HOLDOUT_PROFILE)


def profile_config(name: str = LEGACY_PROFILE) -> dict:
    if name in HOLDOUT_PROFILES:
        selected = {
            "name": name,
            "arms": ["B", "C"] if name == BC_HOLDOUT_PROFILE else ["A", "B", "C"],
            "repetitions": 3,
            "limits": {
                "input_tokens": 9223372036854775807,
                "output_tokens": 9223372036854775807,
                "requests": 2147483647,
                "tests": 2147483647,
                "active_seconds": 3600,
                "timeout_seconds": 180 if name == BC_HOLDOUT_PROFILE else 300,
                "limit_cny": 300.0,
            },
            "per_request_output_tokens": 393216,
            "arm_limit_cny": None,
            "trial_limit_cny": 10.0,
            "input_counter": "official_estimate",
            "capability_mode": True,
        }
        if name == BC_HOLDOUT_PROFILE:
            selected["hard_request_deadline_seconds"] = 180
        return selected
    if name not in {LEGACY_PROFILE, TOKENIZER_PROFILE, CAPABILITY_PROFILE}:
        raise ValueError("unknown comparison profile")
    new = name != LEGACY_PROFILE
    capability = name == CAPABILITY_PROFILE
    return {
        "name": name,
        "arms": ["B", "C"] if new else ["A", "B", "C"],
        "repetitions": 1 if new else 3,
        "limits": {
            "input_tokens": 1250000 if capability else (350000 if new else 60000),
            "output_tokens": 312500 if capability else (60000 if new else 8000),
            "requests": 2147483647 if capability else (100 if new else 20),
            "tests": 2147483647 if capability else (64 if new else 16),
            "active_seconds": 2147483647 if capability else (3600 if new else 900),
            "timeout_seconds": 120 if new else 60,
            "limit_cny": 50.0 if new else 20.0,
        },
        "per_request_output_tokens": 393216 if capability else (8192 if new else 2048),
        "arm_limit_cny": 25.0 if new else None,
        "trial_limit_cny": 2.5 if new else None,
        "input_counter": "official_estimate" if new else "legacy_utf8_bound",
        "capability_mode": capability,
    }


def protocol_profile(protocol: dict) -> dict:
    selected = protocol.get("profile", profile_config())
    if selected != profile_config(selected["name"]):
        raise ValueError("frozen profile changed")
    return selected


class OfficialCounter:
    """Invoke a frozen native or WSL worker; never substitute a tokenizer."""

    def __init__(self, runtime: dict):
        self.runtime = runtime

    def invoke(self, bodies: list[dict] | None = None) -> dict:
        result = subprocess.run(
            self.runtime["command"],
            input=json.dumps({} if bodies is None else {"bodies": bodies}, ensure_ascii=False),
            capture_output=True,
            encoding="utf-8",
            timeout=1800 if bodies and len(bodies) > 1 else 120,
            check=True,
        )
        parsed = json.loads(result.stdout)
        if "identity" in self.runtime and parsed["identity"] != self.runtime["identity"]:
            raise ValueError("official counter runtime identity changed")
        return parsed

    def count(self, kwargs: dict) -> InputBound:
        body = {
            "model": kwargs["model"].split("/")[-1],
            "messages": kwargs["messages"],
            "tools": kwargs.get("tools", []),
            "thinking": kwargs.get("extra_body", {}).get("thinking"),
        }
        return InputBound(**self.invoke([body])["counts"][0])


def calibrate_previous(previous: Path, runtime: dict) -> dict:
    """Replay exactly the previous 212 paid requests without invoking a provider."""
    import hashlib

    ledger = json.loads((previous / "requests.json").read_text(encoding="utf-8"))
    if len(ledger["requests"]) != 212:
        raise ValueError("exact previous 212 requests required")
    bodies, hashes, expected = [], {}, []
    for row in ledger["requests"]:
        if row["status"] != "completed":
            raise ValueError("previous request is not completed")
        path = previous / "provider" / (row["id"] + "-request.json")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if sha != row["request_sha256"]:
            raise ValueError("previous request corrupted")
        response = previous / "provider" / (row["id"] + "-response.json")
        response_sha = hashlib.sha256(response.read_bytes()).hexdigest()
        if response_sha != row["response_sha256"]:
            raise ValueError("previous response corrupted")
        raw = json.loads(response.read_text(encoding="utf-8"))
        if raw["usage"] != row["usage"]:
            raise ValueError("previous usage changed")
        kwargs = json.loads(path.read_text(encoding="utf-8"))
        bodies.append(
            {
                "model": kwargs["model"].split("/")[-1],
                "messages": kwargs["messages"],
                "tools": kwargs.get("tools", []),
                "thinking": kwargs.get("extra_body", {}).get("thinking"),
            }
        )
        hashes[row["id"]] = {"request": sha, "response": response_sha}
        expected.append(row["usage"]["input_tokens"])
    result = OfficialCounter(runtime).invoke(bodies)
    rows = [
        {
            "id": row["id"],
            "count": count,
            "actual": actual,
            "delta": None if count["tokens"] is None else count["tokens"] - actual,
        }
        for row, count, actual in zip(ledger["requests"], result["counts"], expected, strict=True)
    ]
    return {
        "accepted": len(rows) == 212
        and all(r["count"]["status"] == "estimate" and r["delta"] == 0 for r in rows),
        "supplier_calls": 0,
        "ledger_sha256": hashlib.sha256((previous / "requests.json").read_bytes()).hexdigest(),
        "previous": str(previous.resolve()),
        "hashes": hashes,
        "runtime": {**runtime, "identity": result["identity"]},
        "rows": rows,
    }
