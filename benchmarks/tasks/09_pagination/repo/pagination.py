from typing import TypeVar

T = TypeVar("T")


def page_items(items: list[T], page: int, page_size: int) -> list[T]:
    """按从 1 开始的页码返回一页元素。"""
    start = (page - 1) * page_size
    return items[start : start + page_size - 1]
