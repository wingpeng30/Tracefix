"""物流选择器的数据结构。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Order:
    destination: str
    weight_kg: float
    temperature: str = "ambient"


@dataclass(frozen=True)
class Route:
    carrier: str
    regions: frozenset[str]
    max_weight_kg: float
    temperature_modes: frozenset[str]
    price_cents: int
    eta_days: int
    reliability: int
