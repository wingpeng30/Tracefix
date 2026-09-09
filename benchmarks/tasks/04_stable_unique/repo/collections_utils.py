def stable_unique(items: list[str]) -> list[str]:
    """去重并保持元素首次出现的顺序。"""
    return sorted(set(items))
