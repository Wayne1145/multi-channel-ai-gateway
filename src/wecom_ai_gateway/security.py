import base64
import hashlib
import hmac

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
