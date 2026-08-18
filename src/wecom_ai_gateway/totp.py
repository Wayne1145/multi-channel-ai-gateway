"""不依赖外部验证服务的 RFC 6238 TOTP 与恢复码实现。"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

TOTP_PERIOD = 30
TOTP_DIGITS = 6
_RECOVERY_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def generate_totp_secret() -> str:
    """生成 160 位随机 Base32 秘钥，不带填充符。"""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _counter(at_time: float | None = None) -> int:
    return int(time.time() if at_time is None else at_time) // TOTP_PERIOD


def totp_code(secret: str, *, at_time: float | None = None, counter: int | None = None) -> str:
    value = _counter(at_time) if counter is None else counter
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", value), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**TOTP_DIGITS)).zfill(TOTP_DIGITS)


def verify_totp(secret: str, code: str, *, last_counter: int | None = None) -> int | None:
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    current = _counter()
    for candidate in (current - 1, current, current + 1):
        if last_counter is not None and candidate <= last_counter:
            continue
        if hmac.compare_digest(totp_code(secret, counter=candidate), code):
            return candidate
    return None


def recovery_code_hash(code: str) -> str:
    normalized = code.replace("-", "").strip().upper()
    return hashlib.sha256(normalized.encode()).hexdigest()


def generate_recovery_codes(count: int = 10) -> list[str]:
    codes = []
    while len(codes) < count:
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(12))
        code = f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"
        if code not in codes:
            codes.append(code)
    return codes


def otpauth_uri(secret: str, username: str, issuer: str = "Tsukuyomi AI Gateway") -> str:
    label = quote(f"{issuer}:{username}", safe="")
    query = urlencode(
        {"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": 6, "period": 30}
    )
    return f"otpauth://totp/{label}?{query}"