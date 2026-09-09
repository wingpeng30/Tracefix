from shipping import Order, Route, choose_route


def route(name, regions, weight, modes, price, eta=3, reliability=90):
    return Route(name, frozenset(regions), weight, frozenset(modes), price, eta, reliability)


def test_global_route_and_weight_boundary_are_eligible():
    item = route("Global", {"*"}, 5, {"ambient"}, 200)
    assert choose_route(Order("apac", 5), [item]) == item


def test_ties_use_eta_then_reliability_then_name():
    slow = route("A", {"eu"}, 10, {"ambient"}, 100, eta=4, reliability=99)
    fast_low = route("Z", {"eu"}, 10, {"ambient"}, 100, eta=2, reliability=80)
    fast_high_b = route("B", {"eu"}, 10, {"ambient"}, 100, eta=2, reliability=95)
    fast_high_a = route("A2", {"eu"}, 10, {"ambient"}, 100, eta=2, reliability=95)
    assert choose_route(Order("EU", 1), [slow, fast_low, fast_high_b, fast_high_a]) == fast_high_a


def test_temperature_mode_is_exact():
    ambient = route("Ambient", {"eu"}, 10, {"ambient"}, 10)
    assert choose_route(Order("eu", 1, "cold"), [ambient]) is None
