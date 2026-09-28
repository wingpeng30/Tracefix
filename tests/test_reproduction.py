from __future__ import annotations

import hashlib
import json
import socket
import subprocess

import pytest

import tracefix.runtime
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.reproduction import (
    _forbid_provider_access,
    _make_docker_inputs,
    _make_source,
    _source_sha256,
    run_smoke,
)


def test_packaged_reproduction_builds_identity_checked_synthetic_input(tmp_path):
    source = _make_source(tmp_path)
    input_root = _make_docker_inputs(tmp_path / "inputs", source)
    stage = input_root / "tracefix-synthetic"
    manifest = json.loads((stage / "input-manifest.json").read_text(encoding="utf-8"))
    bundle = stage / "source.bundle"
    assert manifest["task_id"] == "tracefix-synthetic"
    assert manifest["source_commit"] == subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True
    ).strip()
    assert manifest["files"]["source.bundle"] == hashlib.sha256(bundle.read_bytes()).hexdigest()
    recipe = json.loads((stage / "recipes" / "tracefix-synthetic.json").read_text())
    assert recipe == {"task_id": "tracefix-synthetic", "environment_variables": {}}
    assert len(_source_sha256()) == 64


def test_fixture_source_commit_is_deterministic(tmp_path):
    commits = []
    for name in ("first", "second"):
        source = _make_source(tmp_path / name)
        commits.append(
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        )
    assert commits[0] == commits[1]


def test_local_zero_provider_reproduction_records_real_control_flow(tmp_path):
    report = run_smoke(tmp_path / "baseline")
    assert report["status"] == "completed"
    assert report["scripted_model_requests"] > 0
    assert report["provider_client_constructions"] == 0
    assert report["provider_request_attempts"] == 0
    assert report["network_connect_attempts"] == 0
    assert report["test_runs"] == 1
    assert report["changed_files"] == ["app.py"]
    assert report["trajectory_validation"]["pytest_returncode"] == 0
    assert report["trajectory_validation"]["tool_names"] == [
        "search_code", "read_file", "apply_patch", "run_tests", "get_git_diff"
    ]


def test_skills_reproduction_records_catalog_and_loaded_content_identity(tmp_path):
    report = run_smoke(tmp_path / "skills", skills_enabled=True)
    assert report["status"] == "completed"
    assert report["skill_catalog"][0][0]["name"] == "tracefix-debugging"
    assert report["skill_activations"][0]["content_sha256"] == report["skill_catalog"][0][0][
        "sha256"
    ]
    assert report["skill_activations"][0]["content_bytes"] > 0
    assert report["trajectory_validation"]["tool_names"][0] == "load_skill"


def test_provider_and_network_fault_injection_is_blocked():
    provider_attempts = []
    network_attempts = []
    with _forbid_provider_access(provider_attempts, network_attempts):
        with pytest.raises(AssertionError, match="construction is forbidden"):
            tracefix.runtime.LiteLLMAdapter(None)
        with pytest.raises(AssertionError, match="provider requests are forbidden"):
            object.__new__(LiteLLMAdapter).complete((), ())
        with pytest.raises(AssertionError, match="network access is forbidden"):
            socket.create_connection(("example.invalid", 443), timeout=0.1)
    assert provider_attempts == ["attempt"]
    assert network_attempts == ["attempt"]
