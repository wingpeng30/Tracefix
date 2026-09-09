"""不同来源共享的事件身份协议。"""


def canonical_identity(source: str, external_id: str) -> tuple[str, str]:
    """来源忽略大小写；外部 ID 同时忽略首尾空白和大小写。"""
    return source.casefold(), external_id.strip().casefold()
