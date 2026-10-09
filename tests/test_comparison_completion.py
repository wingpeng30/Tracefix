"""Sealed funding, no replay, real patches and independent pytest acceptance."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_comparison import fixture_catalog

from tracefix.checkpoint import ProcessLock
from tracefix.comparison import digest, file_sha, read_json, write_json
from tracefix.comparison_campaign import qualify_task
from tracefix.comparison_profiles import AC_HOLDOUT_PROFILE, OfficialCounter, profile_config
from tracefix.models.input_bounds import InputBound

SPEC = importlib.util.spec_from_file_location(
    "complete_comparison", Path(__file__).parents[1] / "scripts/complete_comparison.py"
)
completion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(completion)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    anchor = tmp_path / "anchor"
    old_approval = tmp_path / "old-approval.json"
    profile = profile_config(AC_HOLDOUT_PROFILE)
    rows = [
        {"id": f"{i:03d}", "task_id": "fixture", "arm": arm, "block": (i + 1) // 2, "repetition": 1}
        for i, arm in enumerate(["A", "C", "A", "C"], 1)
    ]
    catalog, _ = fixture_catalog(tmp_path)
    task = qualify_task(read_json(catalog)[0], tmp_path / "qualification")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text(task["issue"], encoding="utf-8")
    task["prompt_path"] = str(prompt)
    previous_approval = {"parent_campaign": str(anchor)}
    write_json(old_approval, previous_approval)
    protocol = {
        "profile": profile,
        "limits": profile["limits"],
        "mode": "offline",
        "schedule": rows,
        "tasks": {"fixture": task},
        "counter_runtime": {"command": []},
        "continuation": {"approval": previous_approval},
    }
    write_json(parent / "protocol.json", protocol)
    write_json(parent / "protocol.sha256.json", {"sha256": digest(protocol)})
    write_json(
        parent / "approval-identity.json",
        {
            "path": str(old_approval),
            "approval_sha256": file_sha(old_approval),
            "child": str(parent),
        },
    )
    write_json(
        anchor.with_name("anchor-continuation.claim.json"),
        {
            "child": str(parent),
            "approval_sha256": file_sha(old_approval),
        },
    )
    request = parent / "provider/001-01-request.json"
    write_json(request, {"messages": []})
    write_json(
        parent / "requests.json",
        {
            "protocol_sha256": digest(protocol),
            "limit_cny": 300,
            "requests": [
                {
                    "id": "001-01",
                    "trial_id": "001",
                    "status": "pending",
                    "reserved_peak_cny": 5,
                    "request_sha256": file_sha(request),
                }
            ],
        },
    )
    write_json(
        parent / "trials/001/record.json",
        {
            **rows[0],
            "status": "unknown_request",
            "finished": False,
        },
    )
    approval = tmp_path / "approval.json"
    service_identity_file = tmp_path / "service-identity.json"
    service_identity = {
        "pid": 2,
        "python": "python39",
        "python_version": "3.9.21",
        "started_at": "now",
        "startup_nonce": "nonce",
        "port": 8768,
        "identity_url": "http://127.0.0.1:8768/id",
        "health_url": "http://127.0.0.1:8768/get",
        "script_sha256": "script",
        "dependency_versions": {"httpbin": "0.10.2"},
    }
    write_json(service_identity_file, service_identity)
    write_json(
        approval,
        {
            "kind": "tracefix_unknown_terminal_suffix",
            "future_provider_unknown": "fail_hold_reserve_continue",
            "user_instruction": "explicitly continue all future provider unknowns",
            "parent_campaign": str(parent),
            "parent_protocol_sha256": file_sha(parent / "protocol.json"),
            "requests_sha256": file_sha(parent / "requests.json"),
            "unknown_reservations": {"001-01": 5},
            "service_identity_file": str(service_identity_file),
            "service_identity_sha256": file_sha(service_identity_file),
        },
    )
    validator = SimpleNamespace(check_driver=lambda _: None, plan=lambda *_: (protocol, {}))
    monkeypatch.setattr(completion, "load_validator", lambda _: validator)
    monkeypatch.setattr(completion, "check_driver", lambda _: None)
    monkeypatch.setattr(
        completion,
        "verify_frozen_environment",
        lambda _anchor, current: {"previous": current, "current": current},
    )
    monkeypatch.setattr(
        OfficialCounter,
        "count",
        lambda *_: InputBound(100, "estimate", "fixture", "identity", "sha"),
    )
    return parent, approval, tmp_path / "child"


def test_real_sealed_delivery_reentry_and_claim(prepared, tmp_path):
    parent, approval, child = prepared
    before = file_sha(parent / "requests.json")
    protocol = completion.prepare(parent, child, approval)
    assert protocol["funding"]["prior_conservative_cny"] == 5
    results = completion.run(child, None)
    assert len(results) == 3 and all(r["passed"] and r["finished"] for r in results)
    hashes = [file_sha(p) for p in sorted(child.glob("segments/*/requests.json"))]
    assert completion.run(child, None) == results
    assert hashes == [file_sha(p) for p in sorted(child.glob("segments/*/requests.json"))]
    assert file_sha(parent / "requests.json") == before
    copied = tmp_path / "copied.json"
    copied.write_bytes(approval.read_bytes())
    with pytest.raises(ValueError, match="claimed"):
        completion.prepare(parent, tmp_path / "other", copied)
    with ProcessLock(child), pytest.raises(Exception, match="lock"):
        completion.run(child, None)


def test_unknown_holds_reserve_and_continues_without_replay(prepared, monkeypatch):
    parent, approval, child = prepared
    completion.prepare(parent, child, approval)
    original = completion.execute_trial
    calls = []

    def injected(root, protocol, row, env):
        calls.append(row["id"])
        result = original(root, protocol, row, env)
        if row["id"] != "002":
            return result
        ledger = read_json(root / "requests.json")
        pending = ledger["requests"][-1]
        pending["status"] = "pending"
        pending["reserved_peak_cny"] = 4
        for key in ("response_sha256", "usage", "peak_cost_cny"):
            pending.pop(key, None)
        write_json(root / "requests.json", ledger)
        result.update(status="unknown_request", finished=False)
        result.pop("record_sha256")
        result.pop("artifacts")
        write_json(root / "trials/002/record.json", result)
        raise ValueError("unknown provider result; campaign halted")

    monkeypatch.setattr(completion, "execute_trial", injected)
    results = completion.run(child, None)
    assert calls == ["002", "003", "004"]
    assert results[0]["terminal_provider_unknown"] and results[0]["passed"]
    assert results[0]["status"] == "unknown_request"
    assert read_json(child / "segments/003/protocol.json")["funding"]["prior_conservative_cny"] >= 9
    assert read_json(child / "segments/002/requests.json")["requests"][-1]["status"] == "pending"
    assert completion.run(child, None) == results
    assert calls == ["002", "003", "004"]
    assert (child / "segments/002/trials/002/agent-unknown.json").exists()


@pytest.mark.parametrize("mutation", ["approval", "protocol", "request", "count", "halt"])
def test_anomalies_block_without_sending(prepared, mutation):
    parent, approval, child = prepared
    if mutation == "approval":
        write_json(approval, {**read_json(approval), "future_provider_unknown": "retry"})
    elif mutation == "protocol":
        write_json(parent / "protocol.json", {})
    elif mutation == "request":
        write_json(parent / "provider/001-01-request.json", {})
    elif mutation == "count":
        write_json(
            parent / "requests.json",
            {**read_json(parent / "requests.json"), "halt_reason": "count"},
        )
    else:
        write_json(parent / "halt.json", {"reason": "tool failure"})
    with pytest.raises(ValueError):
        completion.prepare(parent, child, approval)
    assert not list(child.glob("segments/*/requests.json"))


def test_budget_empty_ledger_and_incomplete_no_replay(prepared, monkeypatch):
    parent, approval, child = prepared
    completion.prepare(parent, child, approval)
    original = completion.protocol_profile
    monkeypatch.setattr(
        completion,
        "protocol_profile",
        lambda *args: {**original(*args), "trial_limit_cny": 300},
    )
    assert completion.run(child, None) == []
    assert read_json(child / "budget-stop.json")["sent"] is False
    monkeypatch.setattr(completion, "protocol_profile", original)
    completion.run(child, None, max_trials=1)
    segment = child / "segments/002"
    write_json(segment / "trials/002/record.json", {"status": "started", "finished": False})
    with pytest.raises(ValueError, match="no replay"):
        completion.run(child, None)


def test_driver_and_validator_identity_fail_before_calls(tmp_path):
    with pytest.raises(KeyError):
        completion.check_driver({})
    path = tmp_path / "validator.py"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        completion.load_validator({"parent_driver": str(path), "parent_driver_sha256": "bad"})


def test_service_rebind_rejects_dependency_change():
    old = {
        "pid": 1,
        "started_at": "old",
        "startup_nonce": "a",
        "identity_url": "old-url",
        "port": 8768,
        "health_url": "http://127.0.0.1:8768/get",
        "script_sha256": "same",
        "dependency_versions": {"httpbin": "0.10.2"},
    }
    new = {
        **old,
        "pid": 2,
        "started_at": "new",
        "startup_nonce": "b",
        "identity_url": "new-url",
        "dependency_versions": {"httpbin": "0.11"},
    }
    with pytest.raises(ValueError, match="environment changed"):
        completion.verify_restarted_service(old, new)
