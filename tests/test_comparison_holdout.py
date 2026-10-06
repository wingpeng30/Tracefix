"""Real product snapshots, balanced allocation and honest holdout statistics."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from test_comparison import PATCH, fixture_catalog, messages, provider_config

from tracefix.comparison import (
    ComparisonBudget,
    read_json,
    schedule,
    static_material,
    summarize,
    write_json,
)
from tracefix.comparison_campaign import FixtureClient, execute_trial, finalize_trial, qualify_task
from tracefix.comparison_holdout import aggregate, balanced_schedule, product_diff
from tracefix.comparison_profiles import HOLDOUT_PROFILE, OfficialCounter, profile_config
from tracefix.models.input_bounds import InputBound


def ids():
    return [f"repo{i % 4}__task-{i}" for i in range(20)]


def test_balanced_holdout_schedule_and_profile():
    selected = profile_config(HOLDOUT_PROFILE)
    rows = schedule(selected, ids())
    assert rows == balanced_schedule(ids()) and len(rows) == 180
    orders = Counter(tuple(r["arm"] for r in rows[i : i + 3]) for i in range(0, 180, 3))
    assert len(orders) == 6 and set(orders.values()) == {10}
    assert set(Counter((r["task_id"], r["arm"]) for r in rows).values()) == {3}
    assert selected["arm_limit_cny"] is None and selected["trial_limit_cny"] == 10
    assert selected["limits"]["active_seconds"] == 3600
    with pytest.raises(ValueError, match="twenty"):
        balanced_schedule(ids()[:10])


def test_bare_uses_full_output_allowance_and_no_arm_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(
        OfficialCounter,
        "count",
        lambda *args: InputBound(100, "estimate", "fixture", "identity", "sha"),
    )
    llm = ComparisonBudget(
        provider_config(),
        root=tmp_path,
        trial={"id": "001", "arm": "A"},
        protocol_sha="holdout",
        profile=profile_config(HOLDOUT_PROFILE),
        counter_runtime={"command": []},
        client=FixtureClient(PATCH),
    )
    llm.complete(messages())
    row = llm.ledger()["requests"][0]
    assert row["output_limit"] == 393216 and row["official_count_delta"] == 0
    assert llm.counter_seconds >= 0


def test_cumulative_input_can_cross_previous_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(
        OfficialCounter,
        "count",
        lambda *_: InputBound(500000, "estimate", "fixture", "identity", "sha"),
    )
    llm = ComparisonBudget(
        provider_config(),
        root=tmp_path,
        trial={"id": "001", "arm": "B"},
        protocol_sha="holdout",
        profile=profile_config(HOLDOUT_PROFILE),
        counter_runtime={"command": []},
        client=FixtureClient(PATCH),
    )
    monkeypatch.setattr(llm, "count_input_tokens", lambda *_: 500000)
    for _ in range(3):
        llm.complete(messages())
    assert sum(r["usage"]["input_tokens"] for r in llm.ledger()["requests"]) == 1500000


@pytest.mark.parametrize("failure", ["changed", "missing", "model", "aborted", "unknown_finish"])
def test_known_supplier_identity_failure_settles_and_blocks_replay(tmp_path, monkeypatch, failure):
    from tracefix.exceptions import LLMProviderError

    monkeypatch.setattr(
        OfficialCounter,
        "count",
        lambda *_: InputBound(100, "estimate", "fixture", "identity", "sha"),
    )
    write_json(tmp_path / "protocol.json", {"mode": "live", "fixture_client_no_http": True})

    class Responses(FixtureClient):
        def completion(self, **kwargs):
            result = super().completion(**kwargs)
            result["model"] = "deepseek-flash"
            result["system_fingerprint"] = "stable"
            if self.calls == 2:
                if failure == "changed":
                    result["system_fingerprint"] = "changed"
                elif failure == "missing":
                    result["system_fingerprint"] = None
                elif failure == "model":
                    result["model"] = "unexpected-model"
                elif failure == "aborted":
                    result["choices"][0]["finish_reason"] = "aborted"
                else:
                    result["choices"][0]["finish_reason"] = None
            return result

    client = Responses(PATCH)
    llm = ComparisonBudget(
        provider_config(),
        root=tmp_path,
        trial={"id": "001", "arm": "B"},
        protocol_sha="fixture",
        profile=profile_config(HOLDOUT_PROFILE),
        counter_runtime={"command": []},
        client=client,
    )
    llm.complete(messages())
    with pytest.raises(LLMProviderError):
        llm.complete(messages())
    ledger = read_json(tmp_path / "requests.json")
    assert ledger["halt_reason"] and ledger["supplier_system_fingerprint"] == "stable"
    assert all(r["status"] == "completed" and r["peak_cost_cny"] > 0 for r in ledger["requests"])
    with pytest.raises(LLMProviderError, match="halted"):
        llm.complete(messages())
    assert client.calls == 2
    protocol = {
        "mode": "offline",
        "profile": profile_config(HOLDOUT_PROFILE),
        "tasks": {task: {} for task in ids()},
        "schedule": schedule(profile_config(HOLDOUT_PROFILE), ids()),
    }
    summary = summarize(protocol, [], ledger["requests"])
    assert "deepseek-flash" in summary["supplier_models"]
    assert "stable" in summary["supplier_system_fingerprints"]
    assert summary["supplier_finish_reasons"]["stop"] >= 1


def test_first_block_audit_refuses_halted_ledger(tmp_path, monkeypatch):
    import tracefix.comparison_campaign as campaign

    write_json(tmp_path / "requests.json", {"halt_reason": "identity changed"})
    monkeypatch.setattr(campaign, "validate_requests", lambda *_: [])
    with pytest.raises(ValueError, match="ledger halted"):
        campaign.audit_first_block(tmp_path, {}, ["001", "002", "003"])


@pytest.mark.parametrize("error", ["transport", "format", "interrupt"])
def test_unknown_response_records_elapsed_time_without_settlement(tmp_path, monkeypatch, error):
    import time
    from types import SimpleNamespace

    from tracefix.exceptions import LLMProviderError, LLMResponseFormatError, LLMTimeoutError

    error_type = {
        "transport": LLMTimeoutError,
        "format": LLMResponseFormatError,
        "interrupt": KeyboardInterrupt,
    }[error]
    monkeypatch.setattr(
        OfficialCounter,
        "count",
        lambda *_: InputBound(100, "estimate", "fixture", "identity", "sha"),
    )

    class Unknown(FixtureClient):
        exceptions = SimpleNamespace(Timeout=TimeoutError)

        def completion(self, **kwargs):
            time.sleep(0.03)
            if error == "format":
                result = super().completion(**kwargs)
                result["usage"] = None
                return result
            self.calls += 1
            raise (TimeoutError if error == "transport" else KeyboardInterrupt)(
                "explicit offline failure"
            )

    client = Unknown(PATCH)
    llm = ComparisonBudget(
        provider_config(),
        root=tmp_path,
        trial={"id": "001", "arm": "B"},
        protocol_sha="fixture",
        profile=profile_config(HOLDOUT_PROFILE),
        counter_runtime={"command": []},
        client=client,
    )
    with pytest.raises(error_type):
        llm.complete(messages())
    row = read_json(tmp_path / "requests.json")["requests"][0]
    assert row["status"] == "pending" and "usage" not in row
    assert row["seconds"] >= 0.02 and row["reserved_peak_cny"] > 0
    with pytest.raises(LLMProviderError, match="unknown"):
        llm.complete(messages())
    assert client.calls == 1


def test_unmeasured_interruption_does_not_manufacture_time_advantage():
    records = [
        {
            "id": "001",
            "status": "unknown_request",
            "timings": {
                "preparation_seconds": 2,
                "delivery_seconds": 3,
            },
        },
        {"id": "002", "status": "started"},
        {
            "id": "003",
            "status": "completed",
            "passed": True,
            "timings": {
                "preparation_seconds": 1,
                "delivery_seconds": 3,
            },
        },
    ]
    result = aggregate(records, [])
    assert result["seconds"] == 6 and result["total_wall_seconds"] == 9
    assert not result["time_complete"] and result["unmeasured_run_durations"] == 1
    assert result["seconds_per_success"] is result["total_wall_seconds_per_success"] is None
    assert result["median_seconds"] is None


@pytest.mark.parametrize(
    "failure", ["balance", "reservation_identity", "infrastructure", "unknown"]
)
def test_holdout_run_stops_before_unfair_or_unknown_calls(tmp_path, monkeypatch, failure):
    import tracefix.comparison_campaign as campaign

    protocol = {
        "mode": "offline",
        "profile": profile_config(HOLDOUT_PROFILE),
        "tasks": {t: {} for t in ids()},
        "schedule": balanced_schedule(ids()),
    }
    monkeypatch.setattr(campaign, "check_protocol", lambda *_: protocol)
    requests = [
        {
            "status": "pending" if failure == "unknown" else "completed",
            "peak_cost_cny": 280 if failure == "balance" else 0,
        }
    ]
    monkeypatch.setattr(campaign, "validate_requests", lambda *_: requests)
    calls = []

    def execute(*args):
        calls.append(args[2])
        return {**args[2], "passed": False, "status": "failed", "infrastructure_failure": True}

    monkeypatch.setattr(campaign, "execute_trial", execute)
    if failure == "reservation_identity":
        write_json(tmp_path / "blocks/01.json", {"protocol_sha256": "changed"})
    if failure == "balance":
        assert campaign.run(tmp_path) == [] and not calls
        assert read_json(tmp_path / "budget-stop.json")["sent"] is False
    else:
        with pytest.raises(ValueError):
            campaign.run(tmp_path)
        assert len(calls) == (1 if failure == "infrastructure" else 0)
        if failure == "infrastructure":
            with pytest.raises(ValueError, match="halted"):
                campaign.run(tmp_path)


def test_real_snapshot_keeps_products_and_removes_owned_temps(tmp_path):
    catalog, _ = fixture_catalog(tmp_path)
    source = Path(read_json(catalog)[0]["source"])
    (source / "sample.py").write_text("def add(a, b):\n    return a + b\n")
    (source / "new.bin").write_bytes(bytes(range(256)))
    (source / "test_sample.py").unlink()
    (source / ".tracefix-test-tmp").mkdir()
    (source / ".tracefix-test-tmp" / "ignored.bin").write_bytes(bytes(range(256)))
    diff, changed = product_diff(source, {".tracefix-test-tmp"})
    assert "GIT binary patch" in diff and "new.bin" in changed
    assert "test_sample.py" in changed and ".tracefix-test-tmp" not in diff
    # The caller's index is untouched, including intent-to-add state.
    import subprocess

    assert not subprocess.check_output(["git", "diff", "--cached"], cwd=source)


def test_full_shared_material_is_issue_only_and_complete(tmp_path):
    content = "value = 1\n" * 1000
    (tmp_path / "sample.py").write_text(content)
    (tmp_path / "test_answer.py").write_text("secret")
    result = static_material(tmp_path, "sample", ["sample.py", "test_answer.py"], full_files=True)
    assert content in result and "secret" not in result


def test_verifier_exception_is_durable_infrastructure_failure(tmp_path, monkeypatch):
    import tracefix.comparison_campaign as campaign

    patch = tmp_path / "patch.diff"
    patch.write_text(PATCH)
    from tracefix.comparison import file_sha

    monkeypatch.setattr(
        campaign, "verify", lambda *args: (_ for _ in ()).throw(OSError("injected"))
    )
    record = {
        "patch_sha256": file_sha(patch),
        "holdout_protocol": True,
        "status": "completed",
        "timings": {"agent_seconds": 1.0},
    }
    result = finalize_trial(tmp_path, record, {})
    assert result["finished"] and not result["passed"] and result["infrastructure_failure"]
    assert result["verification_status"] == "failed"
    assert result["timings"]["delivery_seconds"] >= 1
    assert (tmp_path / "verification/infrastructure-error.json").exists()


@pytest.mark.parametrize("arm", ["A", "B", "C"])
def test_preparation_value_error_is_infrastructure_failure(tmp_path, monkeypatch, arm):
    import time

    import tracefix.comparison_campaign as campaign

    catalog, _ = fixture_catalog(tmp_path)
    spec = read_json(catalog)[0]
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Fix add")
    spec["prompt_path"] = str(prompt)
    monkeypatch.setattr(campaign, "protocol_profile", lambda *_: profile_config(HOLDOUT_PROFILE))

    def unavailable(*_):
        time.sleep(0.03)  # Exceed Windows' monotonic clock resolution.
        raise ValueError("workspace unavailable")

    monkeypatch.setattr(campaign.TraceFixRunner, "_clone_repository", unavailable)
    result = execute_trial(
        tmp_path / "campaign",
        {"mode": "offline", "tasks": {"one": spec}},
        {"id": "001", "task_id": "one", "arm": arm, "repetition": 1},
        None,
    )
    assert result["finished"] and result["infrastructure_failure"]
    assert result["agent_status"] == "failed" and not result["passed"]
    assert result["reason"].startswith("preparation:")
    assert result["patch_collection_status"] == "not_attempted"
    assert result["timings"]["preparation_seconds"] > 0


def test_delivery_wall_time_includes_gap_before_verification(tmp_path, monkeypatch):
    import time

    import tracefix.comparison_campaign as campaign
    from tracefix.comparison import file_sha

    patch = tmp_path / "patch.diff"
    patch.write_text(PATCH)
    monkeypatch.setattr(campaign, "verify", lambda *_: {"passed": True})
    result = finalize_trial(
        tmp_path,
        {
            "patch_sha256": file_sha(patch),
            "holdout_protocol": True,
            "status": "completed",
            "prepared_at_unix_seconds": time.time() - 5,
            "timings": {"agent_seconds": 1},
        },
        {},
    )
    assert result["timings"]["delivery_seconds"] >= 5


@pytest.mark.parametrize("arm", ["A", "B", "C"])
def test_holdout_real_agent_trial_timings_and_patch(tmp_path, arm):
    catalog, _ = fixture_catalog(tmp_path)
    spec = qualify_task(read_json(catalog)[0], tmp_path / "qualification")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Fix add")
    spec["prompt_path"] = str(prompt)
    # This trial uses the real byte counter to isolate runner and product snapshots.
    selected = profile_config(HOLDOUT_PROFILE)
    import tracefix.comparison_campaign as campaign

    original = campaign.protocol_profile
    from unittest.mock import patch

    with patch.object(
        campaign, "protocol_profile", lambda *_: {**selected, "input_counter": "legacy_utf8_bound"}
    ):
        result = execute_trial(
            tmp_path / "campaign",
            {"mode": "offline", "tasks": {"one": spec}},
            {"id": "001", "task_id": "one", "arm": arm, "repetition": 1},
            None,
        )
    assert campaign.protocol_profile is original
    assert result["passed"] and result["agent_status"] == "completed"
    assert result["timings"]["preparation_seconds"] > 0
    assert result["timings"]["patch_seconds"] > 0
    assert result["timings"]["delivery_seconds"] > result["timings"]["verification_seconds"]
    root = tmp_path / "campaign"
    request = read_json(root / "requests.json")["requests"][0]
    body = read_json(root / "provider" / f"{request['id']}-request.json")
    assert body["timeout"] == 300 and body["num_retries"] == 0
    assert body["temperature"] == 0 and not body.get("stream", False)
    assert body["extra_body"]["thinking"]["type"] == "disabled"
    assert all(
        tool["function"]["name"] != "load_skill" and not tool["function"]["name"].startswith("mcp")
        for tool in body.get("tools", [])
    )


@pytest.mark.parametrize("arm", ["A", "B", "C"])
def test_final_truncation_keeps_valid_patch_but_is_not_normal_completion(
    tmp_path, monkeypatch, arm
):
    import tracefix.comparison_campaign as campaign
    from tracefix.comparison_holdout import main_success

    catalog, _ = fixture_catalog(tmp_path)
    spec = qualify_task(read_json(catalog)[0], tmp_path / "qualification")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Fix add")
    spec["prompt_path"] = str(prompt)
    selected = {**profile_config(HOLDOUT_PROFILE), "input_counter": "legacy_utf8_bound"}
    monkeypatch.setattr(campaign, "protocol_profile", lambda *_: selected)
    original = FixtureClient.completion

    def truncated(self, **kwargs):
        raw = original(self, **kwargs)
        if raw["choices"][0]["finish_reason"] == "stop":
            raw["choices"][0]["finish_reason"] = "length"
        return raw

    monkeypatch.setattr(FixtureClient, "completion", truncated)
    result = execute_trial(
        tmp_path / "campaign",
        {"mode": "offline", "tasks": {"one": spec}},
        {"id": "001", "task_id": "one", "arm": arm, "repetition": 1},
        None,
    )
    assert result["passed"] and result["supplier_finish_reason"] == "length"
    assert result["status"] == "budget_exhausted" and result["agent_status"] == "completed"
    assert not main_success(result)


def test_inflight_completion_after_active_limit_is_a_stopped_valid_patch(tmp_path, monkeypatch):
    import time

    import tracefix.comparison_campaign as campaign
    from tracefix.comparison_holdout import main_success

    catalog, _ = fixture_catalog(tmp_path)
    spec = qualify_task(read_json(catalog)[0], tmp_path / "qualification")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Fix add")
    spec["prompt_path"] = str(prompt)
    selected = profile_config(HOLDOUT_PROFILE)
    selected["limits"]["active_seconds"] = 1
    selected["input_counter"] = "legacy_utf8_bound"
    monkeypatch.setattr(campaign, "protocol_profile", lambda *_: selected)
    original = FixtureClient.completion

    def delayed(self, **kwargs):
        time.sleep(1.05)
        return original(self, **kwargs)

    monkeypatch.setattr(FixtureClient, "completion", delayed)
    result = execute_trial(
        tmp_path / "campaign",
        {"mode": "offline", "tasks": {"one": spec}},
        {"id": "001", "task_id": "one", "arm": "A", "repetition": 1},
        None,
    )
    assert result["passed"] and result["agent_status"] == "completed"
    assert result["status"] == "budget_exhausted" and result["reason"] == "active_execution_limit"
    assert result["active_time_overrun_seconds"] > 0 and not main_success(result)


def test_summary_counts_stopped_patch_separately_and_bootstraps_tasks():
    selected = profile_config(HOLDOUT_PROFILE)
    protocol = {
        "mode": "offline",
        "profile": selected,
        "tasks": {t: {"repository": f"repo{i % 4}"} for i, t in enumerate(ids())},
        "schedule": schedule(selected, ids()),
    }
    records, requests = [], []
    for row in protocol["schedule"]:
        records.append(
            {
                **row,
                "finished": True,
                "passed": True,
                "status": "completed" if row["arm"] != "B" else "budget_exhausted",
                "seconds": 2,
            }
        )
        requests.append(
            {
                "id": row["id"],
                "trial_id": row["id"],
                "status": "completed",
                "peak_cost_cny": 1,
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        )
    summary = summarize(protocol, records, requests)
    assert summary["complete"] and summary["arms"]["B"]["successes"] == 0
    assert summary["arms"]["B"]["independent_patch_passes"] == 60
    assert summary["arms"]["B"]["cost_per_success_cny"] is None
    assert summary["comparisons"]["C-B"]["percentage_point_difference"] == 100
    assert summary["comparisons"]["C-B"]["bootstrap_95_cost_ratio"] is None
    assert len(summary["by_repository"]) == 4
    assert summarize(protocol, [], [])["comparisons"]["C-A"]["matched_tasks"] == 0
    assert aggregate([], [])["median_seconds"] is None


def test_known_cache_cost_and_unknown_charge_remain_distinct():
    records = [
        {
            "id": "one",
            "status": "completed",
            "passed": False,
            "verification": {"reason": "assertion_failed"},
            "infrastructure_failure": False,
        }
    ]
    requests = [
        {
            "id": "r1",
            "trial_id": "one",
            "status": "completed",
            "peak_cost_cny": 0.001,
            "usage": {"input_tokens": 100, "output_tokens": 5},
            "supplier_raw_usage": {"prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20},
        },
        {"id": "r2", "trial_id": "one", "status": "pending", "reserved_peak_cny": 3},
    ]
    result = aggregate(records, requests)
    assert result["normal_completions"] == 1 and result["successes"] == 0
    assert result["failure_reasons"] == {"assertion_failed": 1}
    assert result["cache_hit_tokens"] == 80 and result["cache_miss_tokens"] == 20
    assert result["reserved_unknown_cny"] == 3 and not result["cost_complete"]
    assert result["supplier_invoice_cny"] is None


def test_large_public_source_skips_complete_file_without_hidden_hints(tmp_path):
    (tmp_path / "large.py").write_text("value = 1\n" * 110000)
    material = static_material(tmp_path, "value", ["large.py"], full_files=True)
    assert "large.py" in material and "完整文件未纳入" in material


@pytest.mark.parametrize("profile_name", [HOLDOUT_PROFILE, "bc-holdout-180s-v1"])
def test_first_complete_block_audit_report_and_crash_reentry(tmp_path, monkeypatch, profile_name):
    """Real tool/pytest block; explicitly synthetic counter, never a live qualification gate."""
    import tracefix.comparison_campaign as campaign
    from tracefix.comparison import digest, file_sha
    from tracefix.models.input_bounds import V41_TOKENIZER_SHA256

    catalog, prices = fixture_catalog(tmp_path)
    specs = read_json(catalog)
    write_json(catalog, [{**specs[0], "task_id": task} for task in ids()])
    tokenizer = tmp_path / "fixture-tokenizer.json"
    tokenizer.write_text("explicit offline counter fixture")
    monkeypatch.setattr(
        campaign, "file_sha", lambda p: V41_TOKENIZER_SHA256 if p == tokenizer else file_sha(p)
    )
    runtime = {"command": [], "identity": {}}
    monkeypatch.setattr(
        campaign,
        "calibrate_previous",
        lambda *_: {"accepted": True, "runtime": runtime, "supplier_calls": 0, "fixture": True},
    )

    def count(self, bodies=None):
        return {
            "identity": {},
            "counts": [
                {
                    "tokens": 100,
                    "status": "estimate",
                    "method": "offline-test-fixture",
                    "identity": "synthetic",
                    "request_sha256": "synthetic",
                }
                for _ in bodies or []
            ],
        }

    monkeypatch.setattr(OfficialCounter, "invoke", count)
    root = tmp_path / "campaign"
    campaign.prepare(
        catalog,
        root,
        prices,
        mode="offline",
        profile=profile_name,
        tokenizer=tokenizer,
        previous_campaign=tmp_path,
    )
    block_size = len(profile_config(profile_name)["arms"])
    result = campaign.run(root, max_trials=block_size)
    assert len(result) == block_size and all(r["passed"] for r in result)
    assert read_json(root / "first-block-audit.json")["accepted"]
    ledger = (root / "requests.json").read_bytes()
    # Simulate interruption between the durable third result and closing its block.
    block = read_json(root / "blocks/01.json")
    block.update(status="reserved", reserved_cny=block_size * 10)
    write_json(root / "blocks/01.json", block)
    (root / "first-block-audit.json").unlink()
    assert len(campaign.run(root, max_trials=block_size)) == block_size
    assert ledger == (root / "requests.json").read_bytes()
    summary = campaign.report(root)
    assert summary["started"] == block_size
    assert summary["unstarted"] == block_size * 59
    assert (root / "per-run.csv").is_file() and (root / "per-task.csv").is_file()
    block["protocol_sha256"] = digest({"changed": True})
    write_json(root / "blocks/01.json", block)
    with pytest.raises(ValueError, match="identity"):
        campaign.run(root)
