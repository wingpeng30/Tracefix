"""Cleanup failure must preserve the original persistence failure and root charge."""

from __future__ import annotations

import pytest
from test_job_remote import setup

from tracefix.messages import ToolCall


def test_receipt_disk_failure_and_executor_abort_failure_keep_primary_and_unknown(
    tmp_path, monkeypatch,
):
    service, ledger, channel, _, attempts = setup(tmp_path)
    with ledger:
        service.start(ToolCall(id="one", name="run_tests", arguments={}))
        channel.finish = True

        def no_space(*args, **kwargs):
            raise OSError("receipt disk full")

        def cannot_abort():
            attempts.append(True)
            raise RuntimeError("executor transport lost")

        monkeypatch.setattr(service.store, "save", no_space)
        service.abort_executor = cannot_abort
        with pytest.raises(OSError, match="receipt disk full") as failure:
            service.shutdown(timeout=0)
        assert "owned executor abort also failed" in failure.value.__notes__
        assert service.has_unknown and ledger.charged == 1 and ledger.held == 0
        assert not service.shutdown(timeout=0) and attempts == [True]
