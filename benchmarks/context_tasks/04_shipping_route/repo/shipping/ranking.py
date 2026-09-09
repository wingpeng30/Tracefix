"""通过硬约束后的稳定业务排序。"""

from shipping.models import Route


def ranking_key(route: Route) -> tuple[int, int, int, str]:
    """价格优先；同价选更快，再选可靠性更高，最后按承运商名稳定排序。"""
    return route.price_cents, route.eta_days, -route.reliability, route.carrier.casefold()
