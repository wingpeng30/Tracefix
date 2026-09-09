"""环境变量来源的规范化规则。"""

ENV_NAMES = {
    "debug": "APP_DEBUG",
    "page_size": "APP_PAGE_SIZE",
    "theme": "APP_THEME",
    "audit_retention_days": "APP_AUDIT_RETENTION_DAYS",
}


def read_environment(environ: dict[str, str]) -> dict[str, str]:
    """空串或仅含空白表示未设置；其余字符串去除首尾空白。"""
    result = {}
    for key, env_name in ENV_NAMES.items():
        value = environ.get(env_name)
        if value is not None and value.strip():
            result[key] = value.strip()
    return result
