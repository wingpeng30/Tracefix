"""事件流合并入口。"""

from dataclasses import replace

from events.identity import canonical_identity
from events.models import Event


def merge_events(events: list[Event]) -> list[Event]:
    """去重后应按每个身份第一次出现的顺序输出。"""
    merged: dict[tuple[str, str], Event] = {}
    for event in events:
        key = canonical_identity(event.source, event.external_id)
        # BUG：最后输入无条件覆盖，且丢失旧 payload 的非冲突字段。
        merged[key] = replace(event, payload=dict(event.payload))
    # BUG：按身份排序破坏第一次出现顺序。
    return [merged[key] for key in sorted(merged)]
