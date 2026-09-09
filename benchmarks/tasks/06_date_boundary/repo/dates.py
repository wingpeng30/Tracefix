from datetime import date


def is_in_window(day: date, start: date, end: date) -> bool:
    """判断日期是否位于包含边界的窗口内。"""
    return start < day < end
