"""最终配置字段的类型转换约定。"""


def coerce(key: str, value: object) -> object:
    if key == "debug" and isinstance(value, str):
        normalized = value.casefold()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
        raise ValueError("debug must be a boolean")
    if key in {"page_size", "audit_retention_days"}:
        return int(value)
    return value
