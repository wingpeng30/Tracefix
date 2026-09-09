"""物流路线选择入口。"""

from shipping.models import Order, Route


def choose_route(order: Order, routes: list[Route]) -> Route | None:
    """没有满足全部硬约束的路线时返回 None。"""
    if not routes:
        return None
    # BUG：当前实现没有先做资格过滤，同价时也只比较了承运商名称。
    return min(routes, key=lambda route: (route.price_cents, route.carrier.casefold()))
