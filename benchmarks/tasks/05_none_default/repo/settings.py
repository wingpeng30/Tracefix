def read_timeout(config: dict[str, int | None]) -> int:
    """读取 timeout，缺失或 None 时使用 30。"""
    return config.get("timeout", 30)
