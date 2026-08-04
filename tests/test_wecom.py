import base64
import hashlib
import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wecom_ai_gateway.config import settings
from wecom_ai_gateway.wecom import decrypt, verify_signature


def encrypt(payload: bytes):
    key = os.urandom(32)
    settings.wecom_encoding_aes_key = base64.b64encode(key).decode().rstrip("=")
    settings.wecom_corp_id = "wwSyntheticCorp"
    plain = os.urandom(16) + struct.pack("!I", len(payload)) + payload + settings.wecom_corp_id.encode()
    pad = 32 - len(plain) % 32
    plain += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(enc.update(plain) + enc.finalize()).decode()


def test_callback_crypto():
    settings.wecom_callback_token = "Token123"
    encrypted = encrypt(b"<xml><Event>kf_msg_or_event</Event></xml>")
    ts = "123"
    nonce = "456"
    sig = hashlib.sha1(
        "".join(sorted([settings.wecom_callback_token, ts, nonce, encrypted])).encode()
    ).hexdigest()
    assert verify_signature(sig, ts, nonce, encrypted)
    assert decrypt(encrypted).startswith(b"<xml>")
    assert not verify_signature("bad", ts, nonce, encrypted)
