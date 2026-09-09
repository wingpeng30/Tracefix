"""重试预算语义。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    # max_retries 不包含第一次调用，因此 max_retries=2 最多执行三次 operation。
    max_retries: int = 2

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
