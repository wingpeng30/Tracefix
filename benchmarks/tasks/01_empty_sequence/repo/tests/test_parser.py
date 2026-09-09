from parser import first_token


def test_first_token_handles_empty_sequence() -> None:
    assert first_token([]) is None


def test_first_token_keeps_normal_behavior() -> None:
    assert first_token(["alpha", "beta"]) == "alpha"
