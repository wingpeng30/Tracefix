import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

from tracefix.ablation import budget_preflight
from tracefix.p2_protocol import P2ProtocolConfig, _agent_configuration, _schedule


def test_four_arm_schedule_and_effective_configuration():
    ids = tuple(f"task-{i}" for i in range(10))
    plans = _schedule(ids, "ablation")
    assert len(plans) == 120 and plans == _schedule(ids, "ablation")
    assert {n for n in Counter((p.task_id, p.arm) for p in plans).values()} == {3}
    assert len({(p.task_id, p.arm, p.repetition) for p in plans}) == 120
    configurations = {}
    for plan in plans:
        config = _agent_configuration(P2ProtocolConfig(design="ablation"), plan)
        assert config.token_optimization_enabled == plan.token_optimization_enabled
        assert config.context.enabled == plan.context_compaction_enabled
        assert config.repo_map.enabled == plan.repo_map_enabled
        configurations[plan.arm.value] = config.model_dump(mode="json")
    baseline = configurations.pop("no_compaction")
    for name, value in configurations.items():
        value["token_optimization_enabled"] = baseline["token_optimization_enabled"]
        value["context"]["enabled"] = baseline["context"]["enabled"]
        value["repo_map"]["enabled"] = baseline["repo_map"]["enabled"]
        assert value == baseline, name


def test_presentation_only_schedule_has_one_isolated_feature() -> None:
    ids = tuple(f"task-{i}" for i in range(10))
    plans = _schedule(ids, "presentation_only")
    assert len(plans) == 60 and plans == _schedule(ids, "presentation_only")
    assert {n for n in Counter((p.task_id, p.arm) for p in plans).values()} == {3}
    expected_orders = (
        ("no_compaction", "tool_presentation_only"),
        ("tool_presentation_only", "no_compaction"),
        ("no_compaction", "tool_presentation_only"),
    )
    for repetition, expected in enumerate(expected_orders, start=1):
        for task_id in ids:
            actual = tuple(
                plan.arm.value
                for plan in plans
                if plan.repetition == repetition and plan.task_id == task_id
            )
            assert actual == expected
    configs = {
        plan.arm.value: _agent_configuration(
            P2ProtocolConfig(design="presentation_only"), plan
        )
        for plan in plans
    }
    baseline, treatment = configs["no_compaction"], configs["tool_presentation_only"]
    assert not baseline.token_optimization_enabled
    assert not treatment.token_optimization_enabled
    assert baseline.tool_result_presentation_enabled is False
    assert treatment.tool_result_presentation_enabled is True
    assert baseline.action_guidance_enabled is treatment.action_guidance_enabled is False
    assert baseline.read_cache_enabled is treatment.read_cache_enabled is False
    assert baseline.repo_map.enabled is treatment.repo_map.enabled is False
    assert baseline.context.enabled is treatment.context.enabled is False


def test_campaign_budget_does_not_reset_for_new_experiment():
    report = budget_preflight(
        cap="100", spent="29.29554120", reserved="0", input_price="2", output_price="8"
    )
    assert report["remaining"] == "70.70445880"
    assert report["development_upper_bound"] == "103.20"
    assert report["both_stages_upper_bound"] == "206.40"
    assert report["both_stages_fully_funded"] is False
    assert report["development_fully_funded"] is False
    assert report["paid_execution_authorized_by_this_report"] is False
    for price in ("NaN", "Infinity", "-1"):
        with pytest.raises(ValueError):
            budget_preflight(
                cap="100", spent="0", reserved="0", input_price=price, output_price="8"
            )


def test_formal_ablation_mock_provider_runs_agent_verification_ledger_and_resume(
    tmp_path, monkeypatch
):
    from test_real_experiment import GOLD, _fixture, _p1_qualification_evidence, _run_git

    import tracefix.p2_protocol as p2
    from tracefix.ablation import summarize_ablation
    from tracefix.messages import Message, MessageRole, ToolCall
    from tracefix.real_recipes import EnvironmentRecipe

    task, source = _fixture(tmp_path)
    sources = tmp_path / "sources"
    sources.mkdir()
    _run_git(tmp_path, "clone", "--quiet", str(source), str(sources / task.id))
    monkeypatch.setattr(p2, "P1_QUALIFIED_TASK_IDS", (task.id,))
    monkeypatch.setattr(p2, "COLLECTION_FAILURE_TASK_IDS", ())
    monkeypatch.setattr(p2, "load_real_issue_tasks", lambda *a, **k: (task,))
    monkeypatch.setattr(
        p2, "load_environment_recipes", lambda *_: {task.id: EnvironmentRecipe(task_id=task.id)}
    )
    monkeypatch.setattr(p2, "resolve_managed_environment_python", lambda *_: Path(sys.executable))
    monkeypatch.setattr(p2, "_tracked_diff", lambda *_: b"")
    monkeypatch.setattr("tracefix.runtime.load_environment_file", lambda *_: None)
    provider_calls = []

    class FakeProvider(p2.P2SimulationLLM):
        def count_input_tokens(self, messages, tools=()):
            return 1000

        def complete(self, messages, tools=()):
            provider_calls.append(1)
            response = super().complete(messages, tools)
            if self.calls == 1:
                response = response.model_copy(
                    update={
                        "message": Message(
                            role=MessageRole.ASSISTANT,
                            tool_calls=(
                                ToolCall(
                                    id="synthetic-fix",
                                    name="apply_patch",
                                    arguments={"patch": GOLD},
                                ),
                            ),
                        )
                    }
                )
            return response

    monkeypatch.setattr(p2, "LiteLLMAdapter", FakeProvider)
    formal = p2.P2FormalRunRequirements(
        model_name="fixture/offline",
        provider="injected-test-provider",
        pricing_source="virtual test prices; not a provider bill",
        total_cost_cap_usd=1,
        input_cost_per_million_usd=2,
        output_cost_per_million_usd=8,
        campaign_ledger_path=tmp_path / "virtual-campaign.json",
    )
    pricing_identity = hashlib.sha256(
        formal.model_dump_json(
            exclude={
                "prior_calculated_amount",
                "prior_unsettled_reservation",
                "campaign_ledger_path",
            }
        ).encode()
    ).hexdigest()
    initial_ledger = p2._read_cost_ledger(
        formal.campaign_ledger_path,
        formal.cap,
        formal.provider,
        formal.model_name,
        formal.currency,
        f"campaign:{pricing_identity}",
        pricing_identity,
    )
    p2._write_cost_ledger(formal.campaign_ledger_path, initial_ledger)
    qualification_path = _p1_qualification_evidence(tmp_path, (task,), ())
    # Keep the full node ID produced by the hidden fixture unchanged in P1
    # qualification evidence and its structured audit artifacts.
    config = P2ProtocolConfig(
        design="ablation",
        source_root=sources,
        formal=formal,
        p1_evidence_path=qualification_path,
    )
    root = tmp_path / "formal-offline"
    verifier = p2.validate_agent_patch_strict
    interruptions = []

    def interrupted(*args, **kwargs):
        if not interruptions:
            interruptions.append(True)
            raise RuntimeError("verification interrupted after Agent persisted")
        return verifier(*args, **kwargs)

    monkeypatch.setattr(p2, "validate_agent_patch_strict", interrupted)
    with pytest.raises(RuntimeError, match="verification interrupted"):
        p2.run_p2_formal(config, experiment_dir=root)
    assert len(provider_calls) == 2
    completed = p2.run_p2_formal(config, experiment_dir=root)
    assert completed.completed_count == 12
    assert len(provider_calls) == 24
    assert all(r.independent_passed for r in completed.results), [
        (r.sequence, r.task_id, r.arm.value, r.status, r.stop_reason, r.verification_path)
        for r in completed.results
        if not r.independent_passed
    ]
    ledger_before_resume = formal.campaign_ledger_path.read_bytes()
    expected_request_cost = (2 + 8) / 1_000_000
    assert completed.calculated_cost_amount == pytest.approx(24 * expected_request_cost)
    assert all(
        r.calculated_cost_amount == pytest.approx(2 * expected_request_cost)
        and r.cost_currency == "USD"
        for r in completed.results
    )
    resumed = p2.run_p2_formal(config, experiment_dir=root)
    assert resumed.resumed_count == 12 and len(provider_calls) == 24
    assert formal.campaign_ledger_path.read_bytes() == ledger_before_resume
    ledger = json.loads(formal.campaign_ledger_path.read_text(encoding="utf-8"))
    assert ledger["request_count"] == 24 and ledger["uncertain_request"] is False
    assert ledger["calculated_spent_amount"] == pytest.approx(24 * expected_request_cost)
    assert ledger["spent_usd"] == pytest.approx(24 * expected_request_cost)
    assert ledger["reserved_amount"] == ledger["reserved_usd"] == 0
    assert all(
        request["calculated_cost_amount"] == pytest.approx(expected_request_cost)
        for request in ledger["requests"]
    )
    summary = summarize_ablation(root)
    assert summary["valid_evidence_count"] == 12
    assert summary["success_count"] == 12
    assert p2.write_p2_summary(root).is_file()
    first_path = root / "trials" / "001.json"
    saved = first_path.read_text(encoding="utf-8")
    first_path.unlink()
    incomplete = summarize_ablation(root)
    assert incomplete["planned_count"] == 12
    assert incomplete["termination_categories"]["unexecuted"] == 1
    assert incomplete["task_results"][0]["arms"]["no_compaction"]["input_tokens"] is None
    altered = json.loads(saved)
    altered["task_id"] = "another-task"
    first_path.write_text(json.dumps(altered), encoding="utf-8")
    with pytest.raises(p2.BenchmarkError, match="trial identity conflicts"):
        p2.run_p2_formal(config, experiment_dir=root)
    first_path.write_text(saved, encoding="utf-8")
    ledger["uncertain_request"] = True
    formal.campaign_ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    with pytest.raises(p2.BenchmarkError, match="reconciliation before resume"):
        p2.run_p2_formal(config, experiment_dir=root)
    assert len(provider_calls) == 24
    ledger.update(uncertain_request=False, spent_usd=1, calculated_spent_amount=1)
    formal.campaign_ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    stopped = p2.run_p2_formal(config, experiment_dir=tmp_path / "new-experiment-same-campaign")
    assert stopped.campaign_stop_reason == "campaign_budget_exhausted"
    assert stopped.planned_count == 12 and not stopped.batch_complete
    assert len(provider_calls) == 24


@pytest.mark.parametrize("command", ["p2-dry-run", "p2-check", "simulation", "formal"])
def test_cli_routes_ablation_design(tmp_path, monkeypatch, command):
    from types import SimpleNamespace

    from tracefix.cli import main

    captured = []
    if command in {"simulation", "formal"}:
        target = f"run_p2_{command}"
        result = SimpleNamespace(
            completed_count=0,
            trial_count=120,
            planned_count=120,
            campaign_stop_reason=None,
            resumed_count=0,
            summary_path="fixture.json",
        )
        argv = ["p2-run", "--mode", command, "--experiment-dir", str(tmp_path)]
        if command == "formal":
            argv += [
                "--model-name",
                "fixture/offline",
                "--provider",
                "synthetic",
                "--pricing-source",
                "virtual",
                "--total-cost-cap-usd",
                "1",
                "--input-cost-per-million-usd",
                "2",
                "--output-cost-per-million-usd",
                "8",
            ]
    else:
        target = "write_p2_dry_run" if command == "p2-dry-run" else "write_p2_check"
        result = tmp_path / "fixture.json"
        argv = [command]
    monkeypatch.setattr(
        f"tracefix.cli.{target}", lambda config, **kwargs: captured.append(config) or result
    )
    assert main([*argv, "--source-root", str(tmp_path), "--design", "ablation"]) == 0
    assert len(captured) == 1 and captured[0].design == "ablation"
