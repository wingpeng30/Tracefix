from lookup import find_value


def test_find_value_ignores_case() -> None:
    assert find_value({"Content-Type": 7}, "content-type") == 7


def test_find_value_returns_none_for_missing_key() -> None:
    assert find_value({"x": 1}, "y") is None
