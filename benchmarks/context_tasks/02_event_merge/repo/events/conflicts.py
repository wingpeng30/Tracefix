"""同身份事件的冲突解决规则。"""

from events.models import Event


def newer(left: Event, right: Event) -> Event:
    """时间戳较大者胜；时间相同时 sequence 较大者胜；完全相同保留先到者。"""
    if right.timestamp > left.timestamp:
        return right
    if right.timestamp == left.timestamp and right.sequence > left.sequence:
        return right
    return left


def merge_payload(previous: Event, winner: Event) -> dict[str, object]:
    """保留旧事件独有字段，但冲突字段必须使用胜者值。"""
    combined = dict(previous.payload)
    combined.update(winner.payload)
    return combined
