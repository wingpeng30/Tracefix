from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from tracefix.checkpoint import CheckpointError
from tracefix.jobs.domain import JobError
from tracefix.jobs.test_budget import TestExecutionLedger as Ledger


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_root_limits_do_not_acquire_ownership_or_create_a_budget(tmp_path, limit):
    with pytest.raises(JobError, match="limit_invalid"):
        Ledger(tmp_path, run_id="r1", limit=limit)
    assert not (tmp_path / "test-executions").exists()


def test_closed_and_stale_epoch_updates_cannot_refund_or_acknowledge_old_work(tmp_path):
    first = Ledger(tmp_path, run_id="r1", limit=1)
    ticket = first.reserve(call_id="call", invocation_sha256="a" * 64)
    first.before_dispatch(ticket.execution_id)
    first.acknowledge_started(ticket.execution_id)
    first.settle(ticket.execution_id, process_started=True, tree_exit_confirmed=True)
    first.close()
    with pytest.raises(JobError, match="closed"):
        first.get(ticket.execution_id)
    with Ledger(tmp_path, run_id="r1", limit=1) as second:
        before = second.checkpoint_identity()
        with pytest.raises(JobError, match="epoch"):
            second.acknowledge_started(ticket.execution_id)
        assert second.charged == 1 and second.held == 0
        assert second.checkpoint_identity() == before


def test_duplicate_persisted_execution_is_rejected_before_ownership_is_reused(tmp_path):
    with Ledger(tmp_path, run_id="r1", limit=2) as ledger:
        ticket = ledger.reserve(call_id="call", invocation_sha256="a" * 64)
        ledger.settle(ticket.execution_id, process_started=False, tree_exit_confirmed=True)
        saved = ledger.store.load()
        payload = dict(saved.payload)
        payload["executions"] = [*payload["executions"], *payload["executions"]]
        ledger.store.save(payload, sequence=saved.sequence + 1)
    with pytest.raises(ValueError, match="duplicate"):
        Ledger(tmp_path, run_id="r1", limit=2)


def test_last_slot_is_reserved_atomically_and_rejection_cannot_dispatch(tmp_path):
    with Ledger(tmp_path, run_id="r1", limit=1) as ledger:
        def reserve(number):
            try:
                return ledger.reserve(call_id=f"call-{number}", invocation_sha256="a" * 64)
            except JobError as error:
                return error.reason_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, range(2)))
        assert sum(isinstance(value, str) for value in results) == 1
        assert ledger.charged == 0 and ledger.held == 1


def test_ack_and_settlement_are_idempotent_and_queries_do_not_charge(tmp_path):
    with Ledger(tmp_path, run_id="r1", limit=2, baseline_runs=1) as ledger:
        ticket = ledger.reserve(call_id="call1", invocation_sha256="a" * 64)
        ledger.before_dispatch(ticket.execution_id)
        ledger.acknowledge_started(ticket.execution_id)
        ledger.acknowledge_started(ticket.execution_id)
        assert ledger.charged == 2 and ledger.held == 0
        ledger.settle(ticket.execution_id, process_started=True, tree_exit_confirmed=True)
        ledger.settle(ticket.execution_id, process_started=True, tree_exit_confirmed=True)
        assert ledger.charged == 2
        with pytest.raises(JobError, match="replay"):
            ledger.before_dispatch(ticket.execution_id)
        with pytest.raises(JobError):
            ledger.reserve(call_id="call1", invocation_sha256="a" * 64)


@pytest.mark.parametrize("crash_boundary", ["reserved", "dispatched", "started"])
def test_restart_preserves_uncertain_reservations_and_cannot_replay(tmp_path, crash_boundary):
    with Ledger(tmp_path, run_id="r1", limit=2) as first:
        ticket = first.reserve(call_id="call1", invocation_sha256="a" * 64)
        if crash_boundary != "reserved":
            first.before_dispatch(ticket.execution_id)
        if crash_boundary == "started":
            first.acknowledge_started(ticket.execution_id)
    with Ledger(tmp_path, run_id="r1", limit=2) as restored:
        assert restored.charged + restored.held == 1
        with pytest.raises(JobError, match="unknown"):
            restored.reserve(call_id="new-call", invocation_sha256="b" * 64)
        with pytest.raises(JobError, match="replay"):
            restored.before_dispatch(ticket.execution_id)


def test_confirmed_failed_start_releases_slot_but_lost_ack_does_not(tmp_path):
    with Ledger(tmp_path, run_id="r1", limit=1) as ledger:
        first = ledger.reserve(call_id="call1", invocation_sha256="a" * 64)
        ledger.before_dispatch(first.execution_id)
        ledger.settle(first.execution_id, process_started=False, tree_exit_confirmed=True)
        assert ledger.charged == ledger.held == 0
        second = ledger.reserve(call_id="call2", invocation_sha256="a" * 64)
        ledger.before_dispatch(second.execution_id)
        ledger.settle(second.execution_id, process_started=None, tree_exit_confirmed=True)
        assert ledger.held == 1
        with pytest.raises(JobError, match="unknown"):
            ledger.reserve(call_id="call3", invocation_sha256="a" * 64)


def test_atomic_write_failure_does_not_return_ticket_or_change_live_budget(tmp_path, monkeypatch):
    with Ledger(tmp_path, run_id="r1", limit=1) as ledger:
        monkeypatch.setattr(ledger.store, "save", lambda *args, **kwargs: (_ for _ in ()).throw(
            CheckpointError("disk full")))
        with pytest.raises(CheckpointError):
            ledger.reserve(call_id="call1", invocation_sha256="a" * 64)
        assert ledger.charged == ledger.held == 0
        with pytest.raises(JobError, match="persistence_failed"):
            ledger.reserve(call_id="retry", invocation_sha256="b" * 64)


def test_second_owner_and_changed_budget_identity_are_rejected(tmp_path):
    with Ledger(tmp_path, run_id="r1", limit=1):
        with pytest.raises(CheckpointError):
            Ledger(tmp_path, run_id="r1", limit=1)
    with pytest.raises(CheckpointError):
        Ledger(tmp_path, run_id="r1", limit=2)


def test_dispatch_must_be_durable_before_spawning_and_start_cannot_be_retracted(tmp_path):
    with Ledger(tmp_path, run_id="r1", limit=1) as ledger:
        ticket = ledger.reserve(call_id="call1", invocation_sha256="a" * 64)
        with pytest.raises(JobError, match="dispatch_not_recorded"):
            ledger.settle(ticket.execution_id, process_started=True, tree_exit_confirmed=True)
        ledger.before_dispatch(ticket.execution_id)
        ledger.acknowledge_started(ticket.execution_id)
        with pytest.raises(JobError, match="cannot_be_retracted"):
            ledger.settle(ticket.execution_id, process_started=False, tree_exit_confirmed=True)


def test_dispatch_write_failure_never_reaches_execution_callback(tmp_path, monkeypatch):
    dispatched = []
    with Ledger(tmp_path, run_id="r1", limit=1) as ledger:
        ticket = ledger.reserve(call_id="call1", invocation_sha256="a" * 64)
        monkeypatch.setattr(ledger.store, "save", lambda *args, **kwargs: (_ for _ in ()).throw(
            CheckpointError("durability unavailable")))
        with pytest.raises(CheckpointError):
            ledger.before_dispatch(ticket.execution_id)
            dispatched.append(ticket.execution_id)
        assert dispatched == []
    with Ledger(tmp_path, run_id="r1", limit=1) as restored:
        assert restored.held == 1
        with pytest.raises(JobError, match="unknown"):
            restored.reserve(call_id="different-call", invocation_sha256="a" * 64)
