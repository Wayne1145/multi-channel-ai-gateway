import base64
import hashlib
import os
import struct

import pytest
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


def test_send_fail_event_marks_outbound_message_failed(database):
    from fastapi.testclient import TestClient

    from wecom_ai_gateway.db import Base, SessionLocal, engine
    from wecom_ai_gateway.main import app
    from wecom_ai_gateway.models import Message, MessageStatus, User

    Base.metadata.create_all(engine)
    client = TestClient(app)
    db = SessionLocal()
    user = User(display_name="FailTarget")
    db.add(user)
    db.flush()
    row = Message(
        channel="wecom_kf",
        channel_instance_id="wkFail",
        external_message_id="out-msg-123",
        direction="outbound",
        message_type="text",
        content="回复内容",
        status=MessageStatus.sent,
        user_id=user.id,
    )
    db.add(row)
    db.commit()
    db.close()

    settings.wecom_callback_token = "Token123"
    payload = (
        "<xml><Event>kf_msg_or_event</Event><Token>tk</Token><OpenKfId>wkFail</OpenKfId>"
        "<MsgId>out-msg-123</MsgId><Status>2</Status><FailReason>用户已删除会话或长时间未回复</FailReason></xml>"
    ).encode()
    encrypted = encrypt(payload)
    ts, nonce = "123", "456"
    sig = hashlib.sha1(
        "".join(sorted([settings.wecom_callback_token, ts, nonce, encrypted])).encode()
    ).hexdigest()

    response = client.post(
        "/wecom/kf/callback",
        params={"msg_signature": sig, "timestamp": ts, "nonce": nonce},
        content=f"<xml><Encrypt>{encrypted}</Encrypt></xml>",
    )

    assert response.status_code == 200
    db = SessionLocal()
    refreshed = db.get(Message, row.id)
    assert refreshed.status == MessageStatus.failed
    assert "用户已删除会话" in (refreshed.error or "")
    db.close()


def test_upload_media_from_bytes_rejects_oversize():
    import asyncio

    from wecom_ai_gateway.wecom import client

    with pytest.raises(ValueError, match="2MB"):
        asyncio.run(client.upload_media_from_bytes("image", b"x" * (2 * 1024 * 1024 + 1)))


def test_upload_media_from_bytes_rejects_bad_type():
    import asyncio

    from wecom_ai_gateway.wecom import client

    with pytest.raises(ValueError, match="媒体类型"):
        asyncio.run(client.upload_media_from_bytes("pdf", b"x"))
