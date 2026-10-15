from __future__ import annotations

import json
import sys

import pytest

import tracefix.docker_backend as backend
from tracefix.capabilities import CapabilityCatalog, CapabilityPolicy
from tracefix.docker_backend import _BridgeSession
from tracefix.exceptions import WorkspaceError
from tracefix.jobs.constants import JOB_CONTROL_CAPABILITIES
from tracefix.jobs.protocol import JobProtocolHello
from tracefix.tools.base import ToolSpec


def header():
    return JobProtocolHello(version=1, worker_epoch="a" * 32, host_owner_epoch="b" * 32,
                             logical_run_id="logical-run", context_sha256="c" * 64).model_dump()


def peer_payload(*, jobs=None, tools=()):
    catalog = CapabilityCatalog.for_tool_names((*[tool.name for tool in tools],
                                               *JOB_CONTROL_CAPABILITIES))
    policy = CapabilityPolicy(catalog, "workspace_write")
    return {"type": "hello", "protocol": 1, "run_id": "physical-run",
            "tools": [tool.model_dump(mode="json") for tool in tools],
            "execution_profile": "workspace_write",
            "capability_catalog_sha256": catalog.sha256,
            "capability_policy_sha256": policy.sha256,
            "executor_policy_config_sha256": "d" * 64,
            "artifact_protocol": {"version": 1, "max_artifact_bytes": 8 * 1024 * 1024,
                                  "max_run_bytes": 64 * 1024 * 1024, "max_run_artifacts": 128},
            "job_protocol": header() if jobs is None else jobs}


def session(tmp_path, payload, **changes):
    command = [sys.executable, "-u", "-c",
               "import sys;print(sys.argv[1],flush=True);sys.stdin.read()", json.dumps(payload)]
    return _BridgeSession(command, 2, tmp_path / "events.jsonl", artifacts_enabled=True,
                           executor_policy_config_sha256="d" * 64,
                           **({"job_protocol": header()} | changes))


def test_exact_job_protocol_is_distinct_from_the_raw_executor_tool_schemas(tmp_path):
    with pytest.raises(WorkspaceError):
        session(tmp_path, peer_payload(), job_protocol=None)
    owned = session(tmp_path, peer_payload())
    try:
        assert owned.run_id == "physical-run" and owned.job_protocol == header()
        assert owned.tools == ()
        expected = CapabilityCatalog.for_tool_names(JOB_CONTROL_CAPABILITIES)
        assert owned.capability_catalog_sha256 == expected.sha256
    finally:
        owned.close()


@pytest.mark.parametrize("change", [
    {"version": True}, {"version": 2}, {"worker_epoch": "e" * 32},
    {"host_owner_epoch": "e" * 32}, {"context_sha256": "e" * 64},
    {"logical_run_id": "other-run"}, {"extra_capability": "shell"},
])
def test_protocol_identity_drift_never_falls_back_to_legacy_execution(tmp_path, change):
    with pytest.raises(WorkspaceError, match="job protocol negotiation"):
        session(tmp_path, peer_payload(jobs=header() | change))


def test_missing_version_or_missing_job_protocol_is_not_implicit_allow(tmp_path):
    incomplete = header()
    del incomplete["version"]
    for jobs in (incomplete, {}):
        with pytest.raises(WorkspaceError, match="job protocol negotiation"):
            session(tmp_path, peer_payload(jobs=jobs))


@pytest.mark.parametrize("name", ["job_start_control", "get_task_state", "mcp_serena_symbols"])
def test_private_controls_host_state_and_mcp_do_not_enter_executor_tool_directory(tmp_path, name):
    spec = ToolSpec(name=name, description="untrusted peer directory entry")
    with pytest.raises(WorkspaceError, match="capability catalog is invalid"):
        session(tmp_path, peer_payload(tools=(spec,)))


def test_invalid_local_expected_protocol_fails_before_starting_a_peer(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(backend.subprocess, "Popen", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(WorkspaceError, match="invalid expected"):
        session(tmp_path, peer_payload(), job_protocol=header() | {"version": True})
    assert calls == []
