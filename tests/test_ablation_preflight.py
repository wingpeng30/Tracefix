import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "ablation_preflight.py"
spec = importlib.util.spec_from_file_location("ablation_preflight", SCRIPT)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def recipe_evidence(tmp_path):
    recipe = tmp_path / "recipe.json"
    recipe.write_text('{"dependency": "1.0"}', encoding="utf-8")
    environment = {
        "python_version": "3.9.21",
        "dependency_versions": {"pytest": "7.4.4"},
        "pythonpath_artifacts": {},
    }
    fingerprint = hashlib.sha256(
        json.dumps(environment, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    row = {
        "base_commit": "fixed",
        "environment_before": {
            **environment,
            "fingerprint_sha256": fingerprint,
        },
        "initial_evidence": {"expected_node_ids": ["test.py::test"]},
        "gold_evidence": {"expected_node_ids": ["test.py::test"]},
    }
    entry = {
        "task_id": "task",
        "base_commit": "fixed",
        "recipe_sha256": preflight.sha256(recipe),
        "dependency_fingerprint": fingerprint,
        "target_node_ids": ["test.py::test"],
    }
    return recipe, row, entry


def test_recipe_lock_rejects_edited_recipe(tmp_path):
    recipe, row, entry = recipe_evidence(tmp_path)
    preflight.validate_recipe_lock("task", row, entry, recipe)
    recipe.write_text('{"dependency": "2.0"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="frozen recipe identity changed"):
        preflight.validate_recipe_lock("task", row, entry, recipe)


@pytest.mark.parametrize("field", ["base_commit", "targets", "inventory"])
def test_recipe_lock_rejects_changed_report(tmp_path, field):
    recipe, row, entry = recipe_evidence(tmp_path)
    if field == "base_commit":
        row[field] = "other"
    elif field == "targets":
        row["gold_evidence"]["expected_node_ids"] = ["different"]
    else:
        row["environment_before"]["dependency_versions"]["pytest"] = "8"
    with pytest.raises(RuntimeError):
        preflight.validate_recipe_lock("task", row, entry, recipe)


def test_existing_output_is_never_overwritten(tmp_path):
    output = tmp_path / "run"
    pricing = tmp_path / "pricing.json"
    assert not preflight.prepare_output(output, resume=False, pricing_evidence=pricing)
    evidence = output / "previous.json"
    evidence.write_bytes(b"old evidence")
    with pytest.raises(FileExistsError):
        preflight.prepare_output(output, resume=False, pricing_evidence=pricing)
    assert evidence.read_bytes() == b"old evidence"


def test_resume_only_reuses_frozen_inputs_and_outputs(tmp_path):
    output = tmp_path / "run"
    output.mkdir()
    pricing = tmp_path / "pricing.json"
    pricing.write_text("{}")
    artifact = output / "result.json"
    artifact.write_text("{}")
    preflight.write_new_json(
        output / "preflight-manifest.json",
        {
            "pricing_evidence": str(pricing.resolve()),
            "inputs": {str(pricing): preflight.sha256(pricing)},
            "outputs": {str(artifact): preflight.sha256(artifact)},
        },
    )
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in output.iterdir()}
    assert preflight.prepare_output(output, resume=True, pricing_evidence=pricing)
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in output.iterdir()}
    pricing.write_text('{"changed": true}')
    with pytest.raises(RuntimeError, match="frozen inputs changed"):
        preflight.prepare_output(output, resume=True, pricing_evidence=pricing)


def test_pricing_requires_original_html(tmp_path):
    html = tmp_path / "pricing.html"
    html.write_text("official pricing snapshot")
    pricing = tmp_path / "pricing.json"
    pricing.write_text(
        json.dumps(
            {
                "currency": "CNY",
                "model": "deepseek-flash",
                "local_html": str(html),
                "html_sha256": preflight.sha256(html),
            }
        )
    )
    assert preflight.validate_pricing(pricing)["currency"] == "CNY"
    html.write_text("modified snapshot")
    with pytest.raises(RuntimeError, match="HTML hash mismatch"):
        preflight.validate_pricing(pricing)


def test_legacy_service_cannot_be_attested_by_pid_file(tmp_path):
    pid_file = tmp_path / "server.pid"
    pid_file.write_text("not a pid")
    assert preflight.inspect_service_process(pid_file, 8765)["identity_status"] == "unknown"


def test_windows_venv_listener_must_belong_to_expected_launcher(tmp_path):
    environment, base = tmp_path / "venv", tmp_path / "base"
    launcher, executable = environment / "Scripts" / "python.exe", base / "python.exe"
    suffix = " -m flask --app httpbin:app run --host 127.0.0.1 --port 8765 --no-reload"
    payload = {
        "process": {
            "ProcessId": 10,
            "ExecutablePath": str(launcher),
            "CommandLine": f'"{launcher}"' + suffix,
        },
        "listeners": {"LocalAddress": "127.0.0.1", "LocalPort": 8765, "OwningProcess": 11},
        "owners": {
            "ProcessId": 11,
            "ParentProcessId": 10,
            "ExecutablePath": str(executable),
            "CommandLine": str(executable) + suffix,
        },
    }
    assert preflight.verify_process_chain(payload, environment, base, 8765)
    payload["owners"]["ParentProcessId"] = 99
    assert not preflight.verify_process_chain(payload, environment, base, 8765)


def test_completed_rehearsal_with_fixture_failure_cannot_pass_gate():
    summary = {
        "planned_count": 120,
        "completed_count": 120,
        "valid_evidence_count": 120,
        "infrastructure_count": 0,
        "verification_error_count": 0,
        "evidence_issue_count": 0,
        "success_count": 0,
    }
    preflight.validate_rehearsal_summary(summary)
    # All attempts ended, but twelve could not execute their tests.
    summary.update(valid_evidence_count=108, verification_error_count=12, evidence_issue_count=12)
    with pytest.raises(RuntimeError, match="engineering acceptance failed"):
        preflight.validate_rehearsal_summary(summary)


def test_resume_rejects_changed_qualification_source(tmp_path):
    output = tmp_path / "run"
    output.mkdir()
    pricing, p1 = tmp_path / "price.json", tmp_path / "p1.json"
    preflight.write_new_json(
        output / "preflight-manifest.json",
        {
            "pricing_evidence": str(pricing.resolve()),
            "p1_evidence": str(p1.resolve()),
            "inputs": {},
            "outputs": {},
        },
    )
    with pytest.raises(RuntimeError, match="P1 evidence path changed"):
        preflight.prepare_output(
            output, resume=True, pricing_evidence=pricing, p1_evidence=tmp_path / "other.json"
        )
