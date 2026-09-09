from datetime import date

from dates import is_in_window


def test_window_includes_both_boundaries() -> None:
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)
    assert is_in_window(start, start, end)
    assert is_in_window(end, start, end)


def test_window_rejects_outside_date() -> None:
    assert not is_in_window(date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31))
