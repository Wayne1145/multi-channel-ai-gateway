import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet

from .config import settings


def external_id_hash(value: str) -> str:
    return hmac.new(settings.identity_hmac_key.encode(), value.encode(), hashlib.sha256).hexdigest()


def _fernet():
    key = (
        settings.secret_encryption_key.encode()
        if settings.secret_encryption_key
        else base64.urlsafe_b64encode(hashlib.sha256(settings.identity_hmac_key.encode()).digest())
    )
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


def verify_admin_token(value: str | None) -> bool:
    return bool(value) and hmac.compare_digest(value, settings.admin_token)


def hash_password(password: str) -> str:
    """使用 scrypt 与独立随机盐保存密码；格式包含参数，便于以后升级。"""
    if len(password) < 10:
        raise ValueError("password must contain at least 10 characters")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(derived).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_text, expected_text = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(expected_text.encode())
        actual = hashlib.scrypt(
            password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False
