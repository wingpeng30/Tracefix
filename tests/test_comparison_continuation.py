"""Unknown parent liabilities, immutable suffix execution and real patch delivery."""

import importlib.util
from pathlib import Path

import pytest
from test_comparison import fixture_catalog

from tracefix.checkpoint import ProcessLock
from tracefix.comparison import digest, file_sha, read_json, write_json
from tracefix.comparison_campaign import qualify_task
from tracefix.comparison_profiles import AC_HOLDOUT_PROFILE, OfficialCounter, profile_config
from tracefix.models.input_bounds import InputBound

SPEC = importlib.util.spec_from_file_location(
    "continue_comparison", Path(__file__).parents[1] / "scripts/continue_comparison.py"
)
continuation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(continuation)


@pytest.fixture
def parent(tmp_path, monkeypatch):
    root = tmp_path / "parent"
    profile = profile_config(AC_HOLDOUT_PROFILE)
    rows = [
        {"id": f"{i:03d}", "task_id": "fixture", "arm": a, "block": (i + 1) // 2, "repetition": 1}
        for i, a in enumerate(["A", "C", "A", "C"], 1)
    ]
    protocol = {
        "profile": profile,
        "limits": profile["limits"],
        "mode": "offline",
        "schedule": rows,
        "tasks": {"fixture": {}},
        "counter_runtime": {"command": []},
    }
    write_json(root / "protocol.json", protocol)
    write_json(root / "protocol.sha256.json", {"sha256": digest(protocol)})
    write_json(root / "first-block-audit.json", {"accepted": True})
    request = root / "provider/001-01-request.json"
    write_json(request, {"messages": []})
    write_json(
        root / "requests.json",
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
        root / "trials/001/record.json",
        {
            **rows[0],
            "finished": False,
            "status": "unknown_request",
        },
    )
    approval = tmp_path / "approval.json"
    write_json(
        approval,
        {
            "kind": "tracefix_bounded_continuation",
            "user_instruction": "retain reserve; continue",
            "parent_campaign": str(root.resolve()),
            "parent_protocol_sha256": file_sha(root / "protocol.json"),
            "requests_sha256": file_sha(root / "requests.json"),
            "unknown_reservations": {"001-01": 5},
        },
    )
    monkeypatch.setattr(continuation, "check_driver", lambda *_: None)
    monkeypatch.setattr(continuation, "check_protocol", lambda *_: protocol)
    return root, approval, protocol


def test_suffix_claim_liability_and_parent_immutability(parent, tmp_path):
    root, approval, _ = parent
    child = tmp_path / "child"
    before = file_sha(root / "requests.json")
    result = continuation.prepare(root, child, approval)
    assert result["skipped_unknown_trials"] == ["001"]
    assert [r["id"] for r in result["schedule"]] == ["002", "003", "004"]
    assert result["funding"]["prior_conservative_cny"] == 5
    assert result == continuation.prepare(root, child, approval)
    with pytest.raises(ValueError, match="claimed"):
        continuation.prepare(root, tmp_path / "other", approval)
    copied_approval = tmp_path / "copied-approval.json"
    copied_approval.write_bytes(approval.read_bytes())
    with pytest.raises(ValueError, match="claimed"):
        continuation.prepare(root, tmp_path / "other", copied_approval)
    with pytest.raises(ValueError, match="outside"):
        continuation.prepare(root, root / "other", approval)
    assert continuation.run(child, None, max_trials=0) == []
    assert file_sha(root / "requests.json") == before
    with ProcessLock(child), pytest.raises(Exception, match="lock"):
        continuation.run(child, None, max_trials=0)


@pytest.mark.parametrize("mutation", ["request", "protocol", "reserve", "approval", "record"])
def test_parent_corruption_blocks_without_provider(parent, mutation):
    root, approval, protocol = parent
    record = read_json(approval)
    if mutation == "request":
        write_json(root / "provider/001-01-request.json", {"changed": True})
    elif mutation == "protocol":
        write_json(root / "protocol.json", {**protocol, "mode": "live"})
    elif mutation == "reserve":
        record["unknown_reservations"] = {"001-01": 0}
    elif mutation == "approval":
        record["user_instruction"] = ""
    else:
        write_json(root / "trials/001/record.json", {"finished": True})
    with pytest.raises(ValueError):
        continuation.plan(root, record)


def test_child_unknown_halt_budget_and_tamper(parent, tmp_path, monkeypatch):
    root, approval, _ = parent
    child = tmp_path / "child"
    continuation.prepare(root, child, approval)
    protocol = read_json(child / "protocol.json")
    pending = {
        "protocol_sha256": digest(protocol),
        "limit_cny": 300,
        "requests": [{"id": "002-01", "status": "pending"}],
    }
    write_json(child / "requests.json", pending)
    with pytest.raises(ValueError, match="unknown"):
        continuation.run(child, None)
    write_json(child / "requests.json", {**pending, "requests": []})
    write_json(child / "halt.json", {"reason": "failure"})
    with pytest.raises(ValueError, match="halted"):
        continuation.run(child, None)
    (child / "halt.json").unlink()
    monkeypatch.setattr(continuation, "prior_spend", lambda p: 295 if "continuation" in p else 0)
    assert continuation.run(child, None) == []
    assert not read_json(child / "budget-stop.json")["sent"]
    write_json(child / "continuation.json", {})
    with pytest.raises(ValueError, match="identity"):
        continuation.run(child, None)


def test_continuation_executes_real_patch_pytest_and_reuses(parent, tmp_path, monkeypatch):
    root, approval, protocol = parent
    catalog, _ = fixture_catalog(tmp_path)
    spec = qualify_task(read_json(catalog)[0], tmp_path / "qualification")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text(spec["issue"], encoding="utf-8")
    spec["prompt_path"] = str(prompt)
    protocol["tasks"] = {"fixture": spec}
    write_json(root / "protocol.json", protocol)
    write_json(root / "protocol.sha256.json", {"sha256": digest(protocol)})
    ledger = read_json(root / "requests.json")
    write_json(root / "requests.json", {**ledger, "protocol_sha256": digest(protocol)})
    write_json(
        approval,
        {
            **read_json(approval),
            "parent_protocol_sha256": file_sha(root / "protocol.json"),
            "requests_sha256": file_sha(root / "requests.json"),
        },
    )
    monkeypatch.setattr(
        OfficialCounter,
        "count",
        lambda *_: InputBound(100, "estimate", "fixture", "identity", "sha"),
    )
    child = tmp_path / "child"
    continuation.prepare(root, child, approval)
    before = file_sha(root / "requests.json")
    results = continuation.run(child, None)
    assert len(results) == 3 and all(r["passed"] and r["finished"] for r in results)
    assert all((child / "trials" / r["id"] / "patch.diff").stat().st_size for r in results)
    child_ledger = file_sha(child / "requests.json")
    assert continuation.run(child, None) == results
    assert file_sha(child / "requests.json") == child_ledger
    assert file_sha(root / "requests.json") == before
