from nested import get_nested


def test_missing_path_returns_default() -> None:
    assert get_nested({"user": {}}, ["user", "name"], "unknown") == "unknown"


def test_existing_path_is_returned() -> None:
    assert get_nested({"user": {"name": "Ada"}}, ["user", "name"]) == "Ada"
