"""租户覆盖规则。

字典中不存在的键或值为 None 才代表“继承”；False、0 和空字符串都是租户显式值。
"""


def tenant_value(overrides: dict[str, object], key: str) -> object | None:
    return overrides.get(key)
