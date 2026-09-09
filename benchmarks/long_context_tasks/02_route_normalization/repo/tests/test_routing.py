from routing import normalize_route


def test_route_trims_and_uses_unicode_casefold() -> None:
    assert normalize_route("  STRAẞE/API  ") == "strasse/api"


def test_internal_separator_is_preserved() -> None:
    assert normalize_route("Orders/V2") == "orders/v2"
