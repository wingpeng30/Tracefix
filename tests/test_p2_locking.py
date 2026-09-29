from types import SimpleNamespace

import pytest

import tracefix.p2_protocol as p2
from tracefix.exceptions import BenchmarkError


def test_startup_failure_releases_both_owned_locks(tmp_path, monkeypatch):
    campaign = tmp_path / "campaign.json"
    campaign.write_text("{}", encoding="utf-8")
    formal = p2.P2FormalRunRequirements(
        model_name="fixture/model",
        provider="fixture",
        pricing_source="virtual",
        total_cost_cap_usd=1,
        input_cost_per_million_usd=1,
        output_cost_per_million_usd=1,
        campaign_ledger_path=campaign,
    )
    config = p2.P2ProtocolConfig(formal=formal)
    monkeypatch.setattr(p2, "check_p2_inputs", lambda *a, **k: SimpleNamespace(protocol=None))
    experiment = tmp_path / "experiment"
    experiment.mkdir()
    protocol = experiment / "protocol.json"
    protocol.write_text("invalid retained evidence", encoding="utf-8")
    with pytest.raises(ValueError):
        p2.run_p2_experiment(config, experiment_dir=experiment, mode="formal")
    assert protocol.read_text(encoding="utf-8") == "invalid retained evidence"
    assert not (experiment / ".p2-run.lock").exists()
    assert not campaign.with_suffix(".lock").exists()
    campaign.with_suffix(".lock").write_text("another owner", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="campaign is already running"):
        p2.run_p2_experiment(config, experiment_dir=experiment, mode="formal")
    assert campaign.with_suffix(".lock").read_text(encoding="utf-8") == "another owner"
    assert not (experiment / ".p2-run.lock").exists()


def test_lock_flush_failure_releases_owned_file(tmp_path, monkeypatch):
    lock = tmp_path / "experiment.lock"

    def fail_sync(*args):
        raise OSError("injected disk failure")

    monkeypatch.setattr(p2.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="disk failure"), p2._p2_exclusive_lock(lock, "busy"):
        pytest.fail("execution must not begin before lock is durable")
    assert not lock.exists()
