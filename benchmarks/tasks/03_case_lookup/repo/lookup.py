def find_value(mapping: dict[str, int], key: str) -> int | None:
    """以大小写无关方式查找字符串键。"""
    return mapping.get(key)
