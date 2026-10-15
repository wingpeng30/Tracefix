from __future__ import annotations

from datetime import timedelta

import pytest
from test_job_remote import setup

from tracefix.jobs.domain import JobError, JobStatus
from tracefix.jobs.remote import RemoteJobService
from tracefix.messages import ToolCall
from tracefix.tools.base import ToolResult


def test_active_and_expired_jobs_refuse_before_prepare_or_reservation(tmp_path):
    service, ledger, channel, _, _ = setup(tmp_path)
    with ledger:
        for name, mode in (("read_file", "background"), ("run_tests", "unknown")):
            with pytest.raises(JobError, match="tool_or_mode"):
                service.start(ToolCall(id="bad", name=name, arguments={}), mode=mode)
        assert not channel.calls and ledger.charged == ledger.held == 0
        record = service.start(ToolCall(id="one", name="run_tests", arguments={}))
        before, requests = ledger.checkpoint_identity(), len(channel.calls)
        with pytest.raises(JobError, match="already_active"):
            service.start(ToolCall(id="two", name="run_tests", arguments={}))
        assert ledger.checkpoint_identity() == before and len(channel.calls) == requests
        channel.finish = True
        service.get(record.identity.job_id)
        service._deadline = 0  # Simulate the monotonic root deadline arriving.
        with pytest.raises(JobError, match="expired"):
            service.start(ToolCall(id="expired", name="run_tests", arguments={}))
        assert service.shutdown()


@pytest.mark.parametrize("fault", ["owner", "revision", "deadline", "call_id", "rejected"])
def test_untrusted_status_receipts_cannot_rewrite_known_start_or_extend_deadline(tmp_path, fault):
    service, ledger, channel, _, abort = setup(tmp_path)
    with ledger:
        record = service.start(ToolCall(id="one", name="run_tests", arguments={}))
        original = channel.call

        def changed(name, args):
            result = original(name, args)
            if name != "__job_status__":
                return result
            if fault == "owner":
                result.output["execution"]["owner_epoch"] = "f" * 32
            elif fault == "revision":
                result.output["job"]["revision"] = 0
            elif fault == "deadline":
                result.output["job"]["deadline"] = (
                    record.deadline + timedelta(seconds=1)).isoformat()
            elif fault == "call_id":
                result.output["job"]["identity"]["call_id"] = "forged"
            else:
                return ToolResult(call_id="control", tool_name=name, success=False,
                                  error="worker refused status")
            return result

        channel.call = changed
        current = service.get(record.identity.job_id)
        assert current.status == JobStatus.OUTCOME_UNKNOWN
        assert current.process_started is True and ledger.charged == 1 and ledger.held == 0
        with pytest.raises(JobError, match="known_quiescence"):
            service.checkpoint_identity()
        assert not service.shutdown(timeout=0) and abort == [True]
        assert not service.shutdown(timeout=0) and abort == [True]


def test_intent_write_failure_is_proven_unstarted_and_sends_zero_rpc(tmp_path, monkeypatch):
    service, ledger, channel, _, abort = setup(tmp_path)
    with ledger:
        def fail(*args, **kwargs):
            raise OSError("receipt disk full")

        monkeypatch.setattr(service.store, "save", fail)
        with pytest.raises(OSError, match="disk full"):
            service.start(ToolCall(id="one", name="run_tests", arguments={}))
        assert not channel.calls and ledger.held == ledger.charged == 0
        assert service.has_unknown
        assert not service.shutdown(timeout=0) and abort == [True]


def test_result_storage_failure_still_aborts_owned_executor_and_keeps_charge(tmp_path, monkeypatch):
    service, ledger, channel, _, abort = setup(tmp_path)
    with ledger:
        record = service.start(ToolCall(id="one", name="run_tests", arguments={}))
        channel.finish = True

        def fail(*args, **kwargs):
            raise OSError("result disk full")

        monkeypatch.setattr(service.store, "save", fail)
        with pytest.raises(OSError, match="disk full"):
            service.get(record.identity.job_id)
        assert ledger.charged == 1 and ledger.held == 0 and service.has_unknown
        with pytest.raises(OSError, match="disk full"):
            service.shutdown(timeout=0)
        assert abort == [True]
        assert not service.shutdown(timeout=0) and abort == [True]


def test_read_only_inspection_refuses_pending_receipts_without_mutating_any_store(tmp_path):
    service, ledger, channel, _, _ = setup(tmp_path)
    with ledger:
        options = {"run_id": "root-run", "checkout_id": "b" * 64}
        assert RemoteJobService.inspect_checkpoint_identity(tmp_path / "missing", **options) == {
            "sequence": 0, "payload_sha256": None}
        record = service.start(ToolCall(id="one", name="run_tests", arguments={}))
        before = service.store.path.read_bytes()
        with pytest.raises(JobError, match="pending_unknown"):
            RemoteJobService.inspect_checkpoint_identity(tmp_path / "jobs", **options)
        assert service.store.path.read_bytes() == before
        channel.finish = True
        service.get(record.identity.job_id)
        identity = service.checkpoint_identity()
        assert RemoteJobService.inspect_checkpoint_identity(
            tmp_path / "jobs", **options) == identity


def test_bad_log_identity_never_falls_back_to_another_run_or_reruns_test(tmp_path):
    service, ledger, channel, _, _ = setup(tmp_path)
    with ledger:
        record = service.start(ToolCall(id="one", name="run_tests", arguments={}))
        original = channel.call

        def wrong(name, args):
            result = original(name, args)
            if name == "__job_read__":
                result.output["job_id"] = "job_" + "f" * 32
            return result

        channel.call = wrong
        with pytest.raises(JobError, match="output_identity_mismatch"):
            service.read(record.identity.job_id)
        assert len([name for name, args in channel.calls if name == "__job_start__"]) == 1
        channel.finish = True
        service.get(record.identity.job_id)
        assert service.shutdown()
