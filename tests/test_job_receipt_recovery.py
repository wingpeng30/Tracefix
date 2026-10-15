"""Untrusted persisted receipts must be checked before any worker can be created."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tracefix.artifacts import ArtifactStore
from tracefix.checkpoint import CheckpointStore
from tracefix.jobs.domain import JobError, JobRecord, JobStatus
from tracefix.jobs.executions import TestExecution as Execution
from tracefix.jobs.service import JobService
from tracefix.tools.base import ToolResult


def receipt(tmp_path):
    execution = Execution(execution_id="a" * 32, call_id="test", invocation_sha256="b" * 64,
                          owner_epoch="c" * 32, phase="settled", process_started=True,
                          tree_exit_confirmed=True)
    result = ToolResult(call_id="test", tool_name="run_tests", success=False,
                        error="synthetic storage contract; no test was run")
    digest = hashlib.sha256(json.dumps(result.model_dump(mode="json"), sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    record = JobRecord(identity={"job_id": "job_" + "d" * 32, "run_id": "r1", "call_id": "test",
        "execution_id": execution.execution_id, "invocation_sha256": execution.invocation_sha256},
        created_at=datetime.now(UTC), deadline=datetime.now(UTC) + timedelta(seconds=10),
        status=JobStatus.COMPLETED, process_started=True, tree_exit_confirmed=True,
        outcome_sha256=digest)
    value = {"record": record.model_dump(mode="json"), "capture_id": "e" * 32,
             "result": result.model_dump(mode="json"), "mode": "background"}
    payload = {"jobs": {record.identity.job_id: value}}
    store = CheckpointStore(tmp_path / "jobs", {
        "kind": "job-receipts-v1", "run_id": "r1", "checkout_id": "f" * 64})
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("receipt recovery must never execute or create a scope")

    artifacts = ArtifactStore(tmp_path / "artifacts", run_id="r1", streaming_enabled=True)

    def load(row=execution):
        return JobService(tmp_path / "jobs", run_id="r1", checkout_id="f" * 64,
            ledger=SimpleNamespace(get=lambda execution_id: row, has_unknown=False),
            artifact_store=artifacts, test_tool=None, authorize=forbidden,
            scope_factory=forbidden, evaluate=forbidden, verify_source=forbidden,
            session_deadline=datetime.now(UTC) + timedelta(seconds=10))

    return store, payload, execution, load, calls


@pytest.mark.parametrize("fault,reason", [
    ("owner", "owner_mismatch"), ("key", "identity_mismatch"),
    ("call", "reference_mismatch"), ("hash", "outcome_receipt_mismatch"),
    ("missing_result", "outcome_receipt_mismatch"), ("ledger", "without_settled"),
    ("observations", "observation_mismatch"), ("payload", "receipt_invalid"),
    ("oversize", "limit_exceeded"),
])
def test_corrupt_or_conflicting_recovery_receipts_cannot_reach_execution(tmp_path, fault, reason):
    store, payload, execution, load, calls = receipt(tmp_path)
    key, entry = next(iter(payload["jobs"].items()))
    if fault == "owner":
        entry["record"]["identity"]["run_id"] = "another-run"
    elif fault == "key":
        payload["jobs"] = {"job_" + "0" * 32: entry}
    elif fault == "call":
        entry["record"]["identity"]["call_id"] = "another-call"
    elif fault == "hash":
        entry["record"]["outcome_sha256"] = "0" * 64
    elif fault == "missing_result":
        entry["result"] = None
    elif fault == "ledger":
        execution = execution.model_copy(update={"phase": "unknown"})
    elif fault == "observations":
        entry["record"].update(status="cancelled", process_started=False)
    elif fault == "payload":
        payload = {"jobs": []}
    store.save(payload, sequence=1)
    if fault == "oversize":
        store.path.write_bytes(b"x" * (8 * 1024**2 + 1))
    before = store.path.read_bytes()
    with pytest.raises(JobError, match=reason):
        load(execution)
    assert store.path.read_bytes() == before and not calls


def test_pending_receipt_recovers_as_unknown_without_restarting_or_reconstructing_output(tmp_path):
    store, payload, execution, load, calls = receipt(tmp_path)
    entry = next(iter(payload["jobs"].values()))
    entry["record"].update(status="starting", process_started=None,
                           tree_exit_confirmed=False, outcome_sha256=None)
    entry["result"] = None
    store.save(payload, sequence=1)
    execution = execution.model_copy(update={"phase": "unknown", "tree_exit_confirmed": False})
    service = load(execution)
    record = service.progress_receipts()[0]
    assert record.status == JobStatus.OUTCOME_UNKNOWN and record.process_started is True
    assert service.has_unknown and not service.active and not service.background_completions()
    assert store.load().sequence == 2 and not calls
    with pytest.raises(JobError, match="pending_unknown"):
        JobService.inspect_checkpoint_identity(tmp_path / "jobs", run_id="r1", checkout_id="f" * 64)


def test_safe_receipt_checkpoint_detects_outside_mutation_and_missing_storage(tmp_path):
    store, payload, _, load, calls = receipt(tmp_path)
    empty = load()
    assert empty.checkpoint_identity() == {"sequence": 0, "payload_sha256": None}
    store.save(payload, sequence=1)
    service = load()
    assert service.checkpoint_identity() == JobService.inspect_checkpoint_identity(
        tmp_path / "jobs", run_id="r1", checkout_id="f" * 64)
    store.save(payload, sequence=2)
    with pytest.raises(JobError, match="outside_owner"):
        service.checkpoint_identity()
    store.path.unlink()
    with pytest.raises(JobError, match="checkpoint_missing"):
        service.checkpoint_identity()
    assert not calls
