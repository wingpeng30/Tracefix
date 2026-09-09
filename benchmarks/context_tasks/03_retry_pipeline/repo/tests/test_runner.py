import pytest
from delivery import run_with_retry
from delivery.errors import InvalidPayload, NetworkError
from delivery.policy import RetryPolicy


def test_transient_failures_use_retry_budget_and_backoff_order():
    calls = 0
    sleeps = []

    def operation():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise NetworkError("offline")
        return "ok"

    assert run_with_retry(operation, RetryPolicy(2), sleeps.append) == "ok"
    assert calls == 3
    assert sleeps == [0.1, 0.25]


def test_permanent_error_is_not_retried():
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        raise InvalidPayload("bad")

    with pytest.raises(InvalidPayload):
        run_with_retry(operation, RetryPolicy(3))
    assert calls == 1
