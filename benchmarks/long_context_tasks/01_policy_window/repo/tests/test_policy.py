from policy import resolve_window


def test_upper_boundary_is_included() -> None:
    assert resolve_window(100, 10, 100)


def test_outside_value_is_rejected() -> None:
    assert not resolve_window(101, 10, 100)
