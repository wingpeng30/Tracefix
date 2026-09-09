from pagination import page_items


def test_full_page_contains_page_size_items() -> None:
    assert page_items(list(range(8)), page=2, page_size=3) == [3, 4, 5]


def test_last_partial_page_is_returned() -> None:
    assert page_items(list(range(5)), page=2, page_size=3) == [3, 4]
