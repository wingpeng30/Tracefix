from ranges import numbers_between


def test_numbers_between_is_inclusive() -> None:
    assert numbers_between(2, 5) == [2, 3, 4, 5]


def test_single_value_range() -> None:
    assert numbers_between(3, 3) == [3]
