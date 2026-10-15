from __future__ import annotations

import pytest

from tracefix.checkpoint import CheckpointError, CheckpointStore
from tracefix.jobs.checkpoints import JobCheckpointCoordinator
from tracefix.jobs.domain import JobError
from tracefix.jobs.test_budget import TestExecutionLedger


class Receipts:
    active = False
    has_unknown = False
    cancels = 0

    def progress_receipts(self):
        return ()

    def checkpoint_identity(self):
        if self.active or self.has_unknown:
            raise JobError("not_quiescent")
        return {"sequence": 0, "payload_sha256": None}

    def shutdown(self):
        self.cancels += 1
        raise AssertionError("checkpoint deferral must not cancel a running job")


def test_active_batch_preserves_last_safe_checkpoint_and_records_nonresumable_progress(tmp_path):
    safe = CheckpointStore(tmp_path, {"run_id": "r1"})
    safe.save({"agent": "last safe batch"}, sequence=7)
    before = safe.path.read_bytes()
    service = Receipts()
    with TestExecutionLedger(tmp_path, run_id="r1", limit=1) as ledger:
        coordinator = JobCheckpointCoordinator(tmp_path, run_id="r1", service=service,
                                               ledger=ledger)
        identity = coordinator.safe_identity()
        coordinator.verify_saved(identity)
        assert not coordinator.defer_if_unsafe(safe_sequence=7)
        row = ledger.reserve(call_id="long-test", invocation_sha256="a" * 64)
        ledger.before_dispatch(row.execution_id)
        service.active = True
        for _ in range(2):
            assert coordinator.defer_if_unsafe(safe_sequence=7)
            assert safe.path.read_bytes() == before
            assert ledger.held == 1 and not service.cancels
        progress = coordinator.progress.load(current_identity=coordinator.progress.identity)
        assert progress.sequence == 2
        assert progress.payload["resumable"] is False
        assert progress.payload["last_safe_sequence"] == 7
        assert progress.payload["reason_code"] == "job_active"
        with pytest.raises(CheckpointError, match="active or unknown"):
            coordinator.verify_saved(identity)
    # A different host epoch has no authority to resume or refund the unknown dispatch.
    service.active, service.has_unknown = False, True
    with TestExecutionLedger(tmp_path, run_id="r1", limit=1) as recovered:
        coordinator = JobCheckpointCoordinator(tmp_path, run_id="r1", service=service,
                                               ledger=recovered)
        assert recovered.held == 1 and recovered.has_unknown
        assert coordinator.defer_if_unsafe(safe_sequence=7)
        assert safe.path.read_bytes() == before
        with pytest.raises(CheckpointError, match="active or unknown"):
            coordinator.verify_saved(identity)
        assert coordinator.progress.load(current_identity=coordinator.progress.identity).payload[
            "reason_code"] == "job_outcome_unknown"


def test_new_known_execution_still_prevents_replay_from_older_safe_checkpoint(tmp_path):
    with TestExecutionLedger(tmp_path, run_id="r1", limit=1) as ledger:
        coordinator = JobCheckpointCoordinator(tmp_path, run_id="r1", service=Receipts(),
                                               ledger=ledger)
        old = coordinator.safe_identity()
        row = ledger.reserve(call_id="never-started", invocation_sha256="a" * 64)
        ledger.settle(row.execution_id, process_started=False, tree_exit_confirmed=True)
        assert ledger.charged == ledger.held == 0
        with pytest.raises(CheckpointError, match="changed after"):
            coordinator.verify_saved(old)
        with pytest.raises(CheckpointError, match="changed after"):
            coordinator.verify_saved(None)
        current = coordinator.safe_identity()
        coordinator.verify_saved(current)
    with TestExecutionLedger(tmp_path, run_id="r1", limit=1) as restored:
        JobCheckpointCoordinator(tmp_path, run_id="r1", service=Receipts(), ledger=restored
                                 ).verify_saved(current)
