from settings import read_timeout


def test_none_uses_default_timeout() -> None:
    assert read_timeout({"timeout": None}) == 30


def test_explicit_timeout_is_preserved() -> None:
    assert read_timeout({"timeout": 5}) == 5
