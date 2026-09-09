"""目的地区域的规范化和覆盖约定。"""


def normalize_region(value: str) -> str:
    """用户输入忽略首尾空格和大小写。"""
    return value.strip().casefold()


def serves(regions: frozenset[str], destination: str) -> bool:
    """星号代表全球；普通区域按 normalize_region 比较。"""
    target = normalize_region(destination)
    return "*" in regions or target in {normalize_region(item) for item in regions}
