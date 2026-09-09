from shipping import Order, Route, choose_route


def route(name, regions, weight, modes, price, eta=3, reliability=90):
    return Route(name, frozenset(regions), weight, frozenset(modes), price, eta, reliability)


def test_ineligible_cheapest_route_is_ignored():
    order = Order(" EU ", 5, "cold")
    cheap_wrong = route("Budget", {"us"}, 20, {"ambient"}, 100)
    valid = route("ColdEU", {"eu"}, 5, {"cold"}, 300)
    assert choose_route(order, [cheap_wrong, valid]) == valid


def test_no_eligible_route_returns_none():
    order = Order("eu", 6, "cold")
    assert choose_route(order, [route("Small", {"eu"}, 5, {"cold"}, 100)]) is None
