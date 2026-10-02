from __future__ import annotations

import importlib
import json
import socket
from pathlib import Path

import pytest

from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.regression_replay import (
    RecordedRegressionClient,
    forbid_live_access,
    run_regression_feedback,
)
from tracefix.reproduction import main


def test_package_regression_feedback_real_pytest_and_artifacts(tmp_path):
    summary = run_regression_feedback(tmp_path / "output with spaces")
    assert summary["source_clean"] and summary["feedback_observed"]
    assert summary["test_runs"] == 8 and summary["model_requests"] == 4
    assert summary["provider_cost"] is None
    run = Path(summary["run_path"])
    events = [
        json.loads(line)
        for line in (run / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    calls = [
        event["payload"]["call"]["id"] for event in events if event["event_type"] == "tool_called"
    ]
    results = [
        event["payload"]["result"]["call_id"]
        for event in events
        if event["event_type"] == "tool_returned"
    ]
    assert calls == results
    gates = [
        event["payload"]["status"]
        for event in events
        if event["event_type"] == "validation_gate_completed"
    ]
    assert gates == ["failed", "passed"]
    assert (run / "patch.diff").read_bytes() == Path(summary["export_path"]).read_bytes()
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    assert result["cost_complete"] is False
    assert (Path(summary["source_path"]) / "widget.py").read_text().endswith("return page\n")
    with pytest.raises(FileExistsError):
        run_regression_feedback(tmp_path / "output with spaces")


@pytest.mark.parametrize(
    "flags", [["--skills-enabled"], ["--backend", "docker"], ["--image-id", "sha256:x"]]
)
def test_regression_scenario_rejects_unsupported_combinations(tmp_path, flags):
    with pytest.raises(SystemExit) as error:
        main(["--scenario", "regression-feedback", "--output", str(tmp_path / "new"), *flags])
    assert error.value.code == 2
    assert not (tmp_path / "new").exists()


def test_recorded_client_requires_actual_failure_feedback():
    client = RecordedRegressionClient()
    with pytest.raises(AssertionError, match="schemas"):
        client.completion()
    client.calls = 2
    with pytest.raises(AssertionError, match="feedback"):
        client.completion(tools=[{}], messages=[{"role": "user", "content": "finish"}])
    client.calls = 4
    with pytest.raises(AssertionError, match="additional"):
        client.completion(tools=[{}], messages=[{}])


def test_recorded_guard_forbids_provider_and_network_without_blocking_adapter():
    from tracefix import LLMConfig

    with forbid_live_access():
        assert importlib.import_module("json") is json
        with pytest.raises(AssertionError, match="provider import"):
            _ = LiteLLMAdapter(LLMConfig(model_name="offline/test")).client
        with pytest.raises(AssertionError, match="provider import"):
            __import__("litellm")
        with pytest.raises(AssertionError, match="provider import"):
            importlib.import_module("litellm.fake")
        with pytest.raises(AssertionError, match="network"):
            socket.create_connection(("127.0.0.1", 1))
        with socket.socket() as connection:
            with pytest.raises(AssertionError, match="network"):
                connection.connect(("127.0.0.1", 1))
            with pytest.raises(AssertionError, match="network"):
                connection.connect_ex(("127.0.0.1", 1))


def test_package_environment_failure_never_claims_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tracefix.runtime.TraceFixRunner._prepare_workspace",
        lambda *_args, **_kwargs: {"success": False, "failure": "pytest missing"},
    )
    with pytest.raises(AssertionError, match="repair failed"):
        main(["--scenario", "regression-feedback", "--output", str(tmp_path / "failed")])
    assert not (tmp_path / "failed" / "reproduction.json").exists()
    assert not (tmp_path / "failed" / "export.patch").exists()


def test_cli_dispatches_packaged_scenario(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "tracefix.regression_replay.run_regression_feedback",
        lambda output: {
            "status": "completed",
            "output": str(output),
        },
    )
    assert main(["--scenario", "regression-feedback", "--output", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
