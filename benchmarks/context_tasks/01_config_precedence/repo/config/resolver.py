"""把四层配置解析为最终值。"""

from config.defaults import DEFAULTS
from config.environment import read_environment
from config.schema import coerce
from config.tenant import tenant_value


def resolve_config(
    request: dict[str, object],
    tenant: dict[str, object],
    environ: dict[str, str],
) -> dict[str, object]:
    """优先级应为 request > tenant > environment > defaults。"""
    environment = read_environment(environ)
    resolved = {}
    for key, default in DEFAULTS.items():
        # BUG：or 会把合法的 False、0 和空字符串误判为缺失。
        value = request.get(key) or tenant_value(tenant, key) or environment.get(key) or default
        resolved[key] = coerce(key, value)
    return resolved
