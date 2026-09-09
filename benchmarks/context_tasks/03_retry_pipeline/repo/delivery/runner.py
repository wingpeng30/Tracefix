"""同步投递重试入口。"""

from collections.abc import Callable
from typing import TypeVar

from delivery.backoff import delay_for_retry
from delivery.policy import RetryPolicy

T = TypeVar("T")
DEFAULT_POLICY = RetryPolicy()


def run_with_retry(
    operation: Callable[[], T],
    policy: RetryPolicy = DEFAULT_POLICY,
    sleep: Callable[[float], None] = lambda _: None,
) -> T:
    """执行 operation，在策略允许时重试。"""
    # BUG：range 只执行 max_retries 次，并且所有异常都会被重试。
    for attempt in range(policy.max_retries):
        try:
            return operation()
        except Exception:
            # BUG：attempt 从 0 开始，不符合 delay_for_retry 的 1-based 契约。
            sleep(delay_for_retry(attempt))
    return operation()
