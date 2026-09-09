"""路线硬约束；任何一项不满足都不能参与价格排序。"""

from shipping.models import Order, Route
from shipping.regions import serves


def is_eligible(order: Order, route: Route) -> bool:
    """重量上限为包含边界，温控模式必须精确支持。"""
    return (
        serves(route.regions, order.destination)
        and order.weight_kg <= route.max_weight_kg
        and order.temperature in route.temperature_modes
    )
