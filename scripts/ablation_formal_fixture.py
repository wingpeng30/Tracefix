"""Offline formal-path rehearsal: ten synthetic identities, never real task answers.

Run explicitly; intentionally excluded from normal pytest collection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


def snapshot() -> dict:
    files = [
        *ROOT.glob("src/tracefix/**/*.py"),
        ROOT / "tests/test_real_experiment.py",
        Path(__file__),
    ]
    return {
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "files": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files
        },
        "python": sys.version,
        "executable": sys.executable,
    }


def rehearse(output: Path) -> dict:
    from test_real_experiment import GOLD, _fixture, _p1_qualification_evidence, _run_git

    import tracefix.p2_protocol as p2
    from tracefix.ablation import summarize_ablation
    from tracefix.messages import Message, MessageRole, ToolCall
    from tracefix.real_recipes import EnvironmentRecipe

    output.mkdir(parents=True, exist_ok=False)
    identity = snapshot()
    (output / "execution-identity.json").write_text(
        json.dumps(identity, indent=2), encoding="utf-8"
    )
    task, source = _fixture(output)
    tasks = tuple(task.model_copy(update={"id": f"synthetic__flags-{i:02d}"}) for i in range(10))
    sources = output / "sources"
    sources.mkdir()
    for item in tasks:
        _run_git(output, "clone", "--quiet", str(source), str(sources / item.id))
    qualification = _p1_qualification_evidence(output, tasks, ())
    for artifact in [qualification, *(output / "p1-artifacts").rglob("*.json")]:
        artifact.write_text(
            artifact.read_text(encoding="utf-8").replace("::test_hidden", "::test_fixed"),
            encoding="utf-8",
        )
    calls = []

    class SyntheticProvider(p2.P2SimulationLLM):
        def count_input_tokens(self, messages, tools=()):
            return 1000

        def complete(self, messages, tools=()):
            calls.append(1)
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

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "network or real-provider construction forbidden in synthetic rehearsal"
        )

    formal = p2.P2FormalRunRequirements(
        model_name="fixture/offline",
        provider="synthetic-only",
        pricing_source="virtual USD; no provider bill or payment",
        total_cost_cap_usd=1,
        input_cost_per_million_usd=2,
        output_cost_per_million_usd=8,
        campaign_ledger_path=output / "virtual-campaign.json",
    )
    pricing_id = hashlib.sha256(
        formal.model_dump_json(
            exclude={
                "prior_calculated_amount",
                "prior_unsettled_reservation",
                "campaign_ledger_path",
            }
        ).encode()
    ).hexdigest()
    ledger = p2._read_cost_ledger(
        formal.campaign_ledger_path,
        formal.cap,
        formal.provider,
        formal.model_name,
        formal.currency,
        f"campaign:{pricing_id}",
        pricing_id,
    )
    p2._write_cost_ledger(formal.campaign_ledger_path, ledger)
    config = p2.P2ProtocolConfig(
        design="ablation", source_root=sources, formal=formal, p1_evidence_path=qualification
    )
    experiment = output / "formal"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "connect", forbidden)
        patch.setattr(socket.socket, "connect_ex", forbidden)
        patch.setattr(socket, "create_connection", forbidden)
        patch.setattr("tracefix.models.litellm_adapter.LiteLLMAdapter.__init__", forbidden)
        patch.setattr(p2, "LiteLLMAdapter", SyntheticProvider)
        patch.setattr("tracefix.runtime.load_environment_file", lambda *_: None)
        # Local implementation is uncommitted; bind all actual sources in the
        # before/after manifests instead of treating this as a paid clean run.
        patch.setattr(p2, "_tracked_diff", lambda *_: b"")
        patch.setattr(p2, "P1_QUALIFIED_TASK_IDS", tuple(t.id for t in tasks))
        patch.setattr(p2, "COLLECTION_FAILURE_TASK_IDS", ())
        patch.setattr(p2, "load_real_issue_tasks", lambda *a, **k: tasks)
        patch.setattr(
            p2,
            "load_environment_recipes",
            lambda *_: {t.id: EnvironmentRecipe(task_id=t.id) for t in tasks},
        )
        patch.setattr(p2, "resolve_managed_environment_python", lambda *_: Path(sys.executable))
        first = p2.run_p2_formal(config, experiment_dir=experiment)
        before = formal.campaign_ledger_path.read_bytes()
        resumed = p2.run_p2_formal(config, experiment_dir=experiment)
        assert formal.campaign_ledger_path.read_bytes() == before
        assert first.completed_count == resumed.resumed_count == 120
        assert len(calls) == 240
        assert all(r.independent_passed for r in first.results)
        saved = json.loads(before)
        assert saved["request_count"] == 240 and not saved["uncertain_request"]
        assert saved["reserved_amount"] == saved["reserved_usd"] == 0
        assert saved["calculated_spent_amount"] == pytest.approx(0.0024)
        assert first.calculated_cost_amount == pytest.approx(0.0024)
        assert all(r.calculated_cost_amount == pytest.approx(0.00002) for r in first.results)
        assert all(r["calculated_cost_amount"] == pytest.approx(0.00001) for r in saved["requests"])
        summary = summarize_ablation(experiment)
        assert summary["valid_evidence_count"] == summary["success_count"] == 120
        p2.write_p2_summary(experiment)
        # A separate experiment must continue the same campaign instead of resetting it.
        saved.update(spent_usd=1, calculated_spent_amount=1)
        (output / "ledger-before-cap-check.json").write_bytes(before)
        formal.campaign_ledger_path.write_text(json.dumps(saved), encoding="utf-8")
        with pytest.raises(p2.BenchmarkError, match="stopped before another provider request"):
            p2.run_p2_formal(config, experiment_dir=output / "same-campaign-cap-check")
        assert len(calls) == 240
    final_identity = snapshot()
    (output / "execution-identity-after.json").write_text(
        json.dumps(final_identity, indent=2), encoding="utf-8"
    )
    assert identity == final_identity, "execution code changed during rehearsal"
    report = {
        "completed_at": datetime.now(UTC).isoformat(),
        "synthetic_task_ids": [t.id for t in tasks],
        "interpretation": (
            "Ten identities of one synthetic flags fixture; engineering only, "
            "not repair effectiveness."
        ),
        "qualification": (
            "Synthetic test-helper qualification inputs; "
            "actual strict pytest verification per trial."
        ),
        "completed": 120,
        "strict_verification_passed": 120,
        "resumed": 120,
        "provider_requests": 240,
        "resume_added_requests": 0,
        "paid_requests": 0,
        "virtual_usd_amount": 0.0024,
        "reserved_amount": 0,
        "shared_campaign_cap_blocks_new_experiment": True,
        "execution_snapshot_unchanged": True,
    }
    (output / "rehearsal-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(rehearse(args.output.resolve()), indent=2))
