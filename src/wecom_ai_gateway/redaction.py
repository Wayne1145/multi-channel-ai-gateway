"""持久化错误前的集中脱敏。"""

import re

_QUERY_SECRET = re.compile(
    r"(?i)(access_token|api[_-]?key|token|secret|password)=([^&\s]+)"
)
_AUTH_HEADER = re.compile(r"(?i)(authorization:\s*)([^\r\n]+)")


def redact_error(error: Exception | str, limit: int = 2000) -> str:
    """隐藏 URL 查询参数和认证头中的凭据，再限制持久化长度。"""
    text = str(error)
    text = _QUERY_SECRET.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = _AUTH_HEADER.sub(r"\1[REDACTED]", text)
    return text[:limit]
