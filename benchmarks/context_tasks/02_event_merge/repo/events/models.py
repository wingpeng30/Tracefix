"""事件的公共数据结构。"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Event:
    source: str
    external_id: str
    timestamp: int
    sequence: int
    payload: dict[str, object] = field(default_factory=dict)
