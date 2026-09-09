"""退避表及索引契约。"""

DELAYS = (0.1, 0.25, 0.5, 1.0)


def delay_for_retry(retry_number: int) -> float:
    """retry_number 从 1 开始；超过表长度时固定使用最后一个值。"""
    if retry_number < 1:
        raise ValueError("retry_number starts at 1")
    return DELAYS[min(retry_number - 1, len(DELAYS) - 1)]
