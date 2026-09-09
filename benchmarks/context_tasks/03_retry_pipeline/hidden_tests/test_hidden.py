import pytest
from delivery import run_with_retry
from delivery.errors import HttpError
from delivery.policy import RetryPolicy


def test_zero_retries_still_runs_once():
    calls = []
    assert run_with_retry(lambda: calls.append(1) or "ok", RetryPolicy(0)) == "ok"
    assert calls == [1]


@pytest.mark.parametrize("status", [400, 401, 404])
def test_non_retryable_http_status_runs_once(status):
    calls = []
    with pytest.raises(HttpError):
        run_with_retry(lambda: calls.append(1) or (_ for _ in ()).throw(HttpError(status)))
    assert calls == [1]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retryable_http_status_can_recover(status):
    calls = []
    def operation():
        calls.append(1)
        if len(calls) == 1:
            raise HttpError(status)
        return "ok"
    assert run_with_retry(operation, RetryPolicy(1)) == "ok"
    assert len(calls) == 2
