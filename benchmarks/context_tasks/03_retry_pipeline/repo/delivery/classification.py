"""错误是否允许重试的唯一判定位置。"""

from delivery.errors import HttpError, NetworkError


def is_retryable(error: Exception) -> bool:
    """网络错误、429 和 5xx 可重试；输入错误及其他 4xx 永久失败。"""
    if isinstance(error, NetworkError):
        return True
    return isinstance(error, HttpError) and (error.status == 429 or error.status >= 500)
