def resolve_window(value: int, lower: int, upper: int) -> bool:
    """判断数值是否位于客户可见的策略窗口内。"""
    return lower <= value < upper
