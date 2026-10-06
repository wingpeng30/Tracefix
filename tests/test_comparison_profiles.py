"""Official-count admission, independent money limits and protocol compatibility."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_comparison import PATCH, messages, provider_config

from tracefix.comparison import (
    ComparisonBudget,
    file_sha,
    read_json,
    schedule,
    summarize,
    write_json,
)
from tracefix.comparison_campaign import FixtureClient, config_for
from tracefix.comparison_profiles import (
    CAPABILITY_PROFILE,
    TOKENIZER_PROFILE,
    OfficialCounter,
    calibrate_previous,
    profile_config,
    protocol_profile,
)
from tracefix.exceptions import LLMProviderError, PreRequestBudgetExceeded
from tracefix.models.input_bounds import InputBound


@pytest.fixture
def official(monkeypatch):
    def invoke(self, bodies=None):
        return {
            "identity": {},
            "counts": [
                {
                    "tokens": 100,
                    "status": "estimate",
                    "method": "fixture-official",
                    "identity": "test",
                    "request_sha256": "test",
                }
                for _ in bodies or []
            ],
        }

    monkeypatch.setattr(OfficialCounter, "invoke", invoke)


def budget(tmp_path, name=TOKENIZER_PROFILE, arm="B", identifier="001", client=None):
    return ComparisonBudget(
        provider_config(),
        root=tmp_path,
        trial={"id": identifier, "arm": arm},
        protocol_sha="new",
        profile=profile_config(name),
        counter_runtime={"command": []},
        client=client or FixtureClient(PATCH),
    )


def test_profiles_schedule_and_config(tmp_path):
    for name in (TOKENIZER_PROFILE, CAPABILITY_PROFILE):
        selected = profile_config(name)
        rows = schedule(selected)
        assert len(rows) == 20 and {r["arm"] for r in rows} == {"B", "C"}
        assert [r["arm"] for r in rows[:4]] == ["B", "C", "C", "B"]
        config = config_for(
            {"source": str(tmp_path), "python": "python"}, "fix", tmp_path, None, selected
        )
        assert config.agent_config.max_input_tokens == selected["limits"]["input_tokens"]
        assert config.per_request_output_tokens == selected["per_request_output_tokens"]
        assert config.llm_timeout_seconds == 120
        assert not config.agent_config.skills_enabled
    assert profile_config(TOKENIZER_PROFILE)["limits"]["input_tokens"] == 350000
    assert len(schedule()) == 90 and protocol_profile({}) == profile_config()
    with pytest.raises(ValueError, match="unknown"):
        profile_config("unknown")
    changed = profile_config()
    changed["limits"]["input_tokens"] = 1
    with pytest.raises(ValueError, match="changed"):
        protocol_profile({"profile": changed})


def test_official_count_not_byte_bound_controls_350k(tmp_path, official, monkeypatch):
    llm = budget(tmp_path)
    monkeypatch.setattr(llm, "count_input_tokens", lambda *_: 360000)
    llm.complete(messages())
    row = llm.ledger()["requests"][0]
    assert row["input_bound"] == 360000 and row["input_admission"] == 100
    assert row["official_count_delta"] == 0 and row["official_count"]["status"] == "estimate"
    ledger = llm.ledger()
    ledger["requests"][0]["usage"]["input_tokens"] = 349901
    write_json(tmp_path / "requests.json", ledger)
    with pytest.raises(PreRequestBudgetExceeded):
        llm.complete(messages())
    assert read_json(tmp_path / "trials/001/refusal.json")["sent"] is False


def test_capability_removes_350k_and_sizes_output_to_money(tmp_path, official, monkeypatch):
    llm = budget(tmp_path, CAPABILITY_PROFILE)
    monkeypatch.setattr(llm, "count_input_tokens", lambda *_: 2000)
    llm.complete(messages())
    first = llm.ledger()["requests"][0]
    assert first["output_limit"] > 8192
    assert first["reserved_peak_cny"] <= 2.5
    ledger = llm.ledger()
    ledger["requests"][0]["usage"]["input_tokens"] = 400000
    write_json(tmp_path / "requests.json", ledger)
    llm.complete(messages())
    assert len(llm.ledger()["requests"]) == 2


@pytest.mark.parametrize("scope", ["trial", "arm", "total", "context"])
def test_independent_fee_or_provider_context_stops_before_send(
    tmp_path, official, monkeypatch, scope
):
    llm = budget(tmp_path, CAPABILITY_PROFILE)
    llm.complete(messages())
    ledger = llm.ledger()
    if scope == "context":
        monkeypatch.setattr(
            llm, "count_input_bound", lambda *_: InputBound(1048576, "estimate", "test", "test")
        )
    else:
        ledger["requests"][0]["peak_cost_cny"] = {"trial": 2.5, "arm": 25, "total": 50}[scope]
        if scope in {"arm", "total"}:
            ledger["requests"][0]["trial_id"] = "different"
        if scope == "total":
            ledger["requests"][0]["arm"] = "C"
        write_json(tmp_path / "requests.json", ledger)
    with pytest.raises(PreRequestBudgetExceeded):
        llm.complete(messages())
    assert llm.client.calls == 1
    assert read_json(tmp_path / "trials/001/refusal.json")["sent"] is False


def test_other_arm_has_independent_balance(tmp_path, official):
    first = budget(tmp_path)
    first.complete(messages())
    ledger = first.ledger()
    ledger["requests"][0]["peak_cost_cny"] = 25
    write_json(tmp_path / "requests.json", ledger)
    other = budget(tmp_path, arm="C", identifier="002")
    other.complete(messages())
    assert other.client.calls == 1


@pytest.mark.parametrize("kind", ["missing", "estimate_missing", "difference", "overshoot"])
def test_counter_anomalies_stop_campaign_preserving_known_usage(
    tmp_path, official, monkeypatch, kind
):
    class Difference(FixtureClient):
        def completion(self, **kwargs):
            result = super().completion(**kwargs)
            actual = 350001 if kind == "overshoot" else 101
            result["usage"].update(prompt_tokens=actual, total_tokens=actual + 10)
            return result

    llm = budget(tmp_path, client=Difference(PATCH))
    if kind == "missing":
        llm.counter = None
    elif kind == "estimate_missing":
        monkeypatch.setattr(
            llm, "count_input_bound", lambda *_: InputBound(None, "unavailable", "x", "x")
        )
    if kind == "overshoot":
        monkeypatch.setattr(llm, "count_input_tokens", lambda *_: 500000)
    with pytest.raises(LLMProviderError):
        llm.complete(messages())
    ledger = read_json(tmp_path / "requests.json")
    assert ledger["halt_reason"]
    if kind in {"difference", "overshoot"}:
        assert ledger["requests"][0]["status"] == "completed"
        assert ledger["requests"][0]["response_sha256"]
    else:
        assert ledger["requests"] == [] and llm.client.calls == 0
    with pytest.raises(LLMProviderError, match="halted"):
        llm.complete(messages())


def test_worker_transport_and_identity(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            stdout=json.dumps(
                {
                    "identity": {"version": 1},
                    "counts": [
                        {
                            "tokens": 1,
                            "status": "estimate",
                            "method": "x",
                            "identity": "x",
                            "request_sha256": None,
                        }
                    ],
                }
            )
        ),
    )
    counter = OfficialCounter({"command": ["worker"], "identity": {"version": 1}})
    assert counter.invoke()["identity"] == {"version": 1}
    assert counter.count({"model": "deepseek/deepseek-flash", "messages": []}).tokens == 1
    counter.runtime["identity"] = {"version": 2}
    with pytest.raises(ValueError, match="identity"):
        counter.invoke()


def test_calibration_replays_212_and_refuses_corrupt_evidence(tmp_path, official):
    rows = []
    for index in range(212):
        identifier = str(index)
        request = tmp_path / "provider" / (identifier + "-request.json")
        response = tmp_path / "provider" / (identifier + "-response.json")
        usage = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
        write_json(request, {"model": "deepseek/deepseek-flash", "messages": []})
        write_json(response, {"usage": usage})
        rows.append(
            {
                "id": identifier,
                "status": "completed",
                "usage": usage,
                "request_sha256": file_sha(request),
                "response_sha256": file_sha(response),
            }
        )
    write_json(tmp_path / "requests.json", {"requests": rows})
    assert calibrate_previous(tmp_path, {})["accepted"]
    request.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="request corrupted"):
        calibrate_previous(tmp_path, {})
    rows.pop()
    write_json(tmp_path / "requests.json", {"requests": rows})
    with pytest.raises(ValueError, match="212"):
        calibrate_previous(tmp_path, {})


def test_two_arm_summary_has_completion_metrics_and_paired_tasks():
    selected = profile_config(TOKENIZER_PROFILE)
    rows = [
        {**r, "finished": True, "passed": r["arm"] == "C", "status": "completed"}
        for r in schedule(selected)
    ]
    result = summarize({"profile": selected, "mode": "offline"}, rows, [])
    assert result["planned"] == 20 and result["complete"]
    assert result["arms"]["C"]["completed_and_passed"] == 10
    assert result["arms"]["B"]["normal_completions"] == 10
    assert list(result["comparisons"]) == ["C-B"]
    assert result["comparisons"]["C-B"]["percentage_point_difference"] == 100


def test_worker_identity_and_real_entrypoint(tmp_path, monkeypatch, capsys):
    from tracefix import comparison_counter as worker

    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(worker, "V41_TOKENIZER_SHA256", file_sha(tokenizer))
    monkeypatch.setattr(worker.metadata, "version", lambda *_: "0.1.1")
    package = tmp_path / "recipe.py"
    package.write_text("official frozen implementation", encoding="utf-8")
    monkeypatch.setattr(
        worker.metadata,
        "distribution",
        lambda *_: SimpleNamespace(
            version="0.1.1",
            files=[Path("recipe.py"), Path("RECORD"), Path("../outside")],
            locate_file=lambda name: tmp_path / name,
        ),
    )
    assert worker.identity(tokenizer)["packages"]["deepseek-recipe"]["files"]
    monkeypatch.setattr(
        worker, "count_deepseek_v41_request", lambda _: InputBound(2, "estimate", "x", "x")
    )
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO('{"bodies":[{}]}'))
    assert worker.main(["--tokenizer", str(tokenizer)]) == 0
    assert json.loads(capsys.readouterr().out)["counts"][0]["tokens"] == 2
    tokenizer.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="tokenizer"):
        worker.identity(tokenizer)
    monkeypatch.setattr(worker.metadata, "version", lambda *_: "wrong")
    with pytest.raises(ValueError, match="version"):
        worker.identity(tokenizer)


def test_new_prepare_and_process_reentry(tmp_path, monkeypatch):
    """Real files/pytest/processes with an explicitly synthetic tokenizer worker."""
    import sys

    from test_comparison import fixture_catalog

    import tracefix.comparison_campaign as campaign
    from tracefix.models.input_bounds import V41_TOKENIZER_SHA256

    catalog, prices = fixture_catalog(tmp_path)
    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_text("fixture tokenizer", encoding="utf-8")
    monkeypatch.setattr(
        campaign,
        "file_sha",
        lambda path: V41_TOKENIZER_SHA256 if path == tokenizer else file_sha(path),
    )
    worker = tmp_path / "fake_counter.py"
    worker.write_text(
        "import json,sys\n"
        "request=json.load(sys.stdin)\n"
        "print(json.dumps({'identity':{}, 'counts':[{'tokens':100,'status':'estimate',"
        "'method':'explicit-offline-fixture','identity':'fixture','request_sha256':None}"
        " for _ in request.get('bodies',[])]}))\n",
        encoding="utf-8",
    )
    runtime = {"command": [sys.executable, str(worker)], "identity": {}}
    runtime_path = tmp_path / "runtime.json"
    write_json(runtime_path, runtime)
    previous_prompt = tmp_path / "previous-prompt.txt"
    previous_prompt.write_text("Fix sample.add to return a + b", encoding="utf-8")
    write_json(
        tmp_path / "protocol.json",
        {
            "tasks": {
                row["task_id"]: {
                    "prompt_path": str(previous_prompt),
                    "prompt_sha256": file_sha(previous_prompt),
                }
                for row in read_json(catalog)
            }
        },
    )
    monkeypatch.setattr(
        campaign,
        "calibrate_previous",
        lambda *args: {
            "accepted": True,
            "runtime": runtime,
            "explicit_offline_fixture": True,
        },
    )
    root = tmp_path / "new-campaign"
    with pytest.raises(ValueError, match="requires"):
        campaign.prepare(catalog, root, prices, mode="offline", profile=CAPABILITY_PROFILE)
    protocol = campaign.prepare(
        catalog,
        root,
        prices,
        mode="offline",
        profile=CAPABILITY_PROFILE,
        tokenizer=tokenizer,
        counter_runtime=runtime_path,
        previous_campaign=tmp_path,
    )
    assert protocol["schema_version"] == 2 and len(protocol["schedule"]) == 20
    results = campaign.run(root, max_trials=2)
    assert len(results) == 2 and all(r["passed"] for r in results)
    before = file_sha(root / "requests.json")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tracefix.comparison_campaign",
            "run",
            "--campaign-dir",
            str(root),
            "--max-trials",
            "2",
        ],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert file_sha(root / "requests.json") == before
    report = campaign.report(root)
    assert report["planned"] == 20 and set(report["arms"]) == {"B", "C"}
    ledger = read_json(root / "requests.json")
    ledger["halt_reason"] = "known counter anomaly"
    write_json(root / "requests.json", ledger)
    with pytest.raises(ValueError, match="halted"):
        campaign.run(root)
