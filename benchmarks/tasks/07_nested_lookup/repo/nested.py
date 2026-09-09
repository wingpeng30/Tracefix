from typing import Any


def get_nested(data: dict[str, Any], path: list[str], default: Any = None) -> Any:
    """沿字符串键路径读取嵌套字典，缺失时返回 default。"""
    current: Any = data
    for key in path:
        current = current[key]
    return current
