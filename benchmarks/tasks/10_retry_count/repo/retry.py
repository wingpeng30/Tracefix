from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def run_with_retries(operation: Callable[[], T], max_retries: int) -> T:
    """执行一次操作，并在失败后最多重试 max_retries 次。"""
    last_error: Exception | None = None
    for _ in range(max_retries):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
    assert last_error is not None
    raise last_error
