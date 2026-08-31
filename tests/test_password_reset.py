"""微信可信渠道发起的后台密码找回测试。"""

import hashlib
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from wecom_ai_gateway.db import SessionLocal
from wecom_ai_gateway.main import app
from wecom_ai_gateway.models import (
    Account,
    AuthSession,
    ChannelIdentity,
    Message,
    MessageDirection,
    MessageStatus,
    MfaChallenge,
    PasswordResetToken,
    User,
)
from wecom_ai_gateway.password_reset import (
    PasswordResetError,
    consume_password_reset,
    create_password_reset_token,
)
from wecom_ai_gateway.security import encrypt_secret, hash_password, verify_password
from wecom_ai_gateway.services import _handle_account_command, process_message

client = TestClient(app)


def _account_user(db, username: str = "reset_user") -> tuple[User, Account]:
    user = User(display_name="找回测试", mode="self_service")
    db.add(user)
    db.flush()
    account = Account(
        user_id=user.id,
        username=username,
        password_hash=hash_password("old-strong-pass-123"),
        role="user",
    )
    db.add(account)
    db.commit()
    return user, account


def test_reset_token_is_hashed_short_lived_and_replaces_old(db):
    user, account = _account_user(db)

    first = create_password_reset_token(db, user.id)
    second = create_password_reset_token(db, user.id)

    assert len(second) >= 40 and first != second
    rows = db.query(PasswordResetToken).filter_by(user_id=user.id).all()
    assert len(rows) == 1
    assert rows[0].account_id == account.id
    assert rows[0].token_hash == hashlib.sha256(second.encode()).hexdigest()
    assert second not in rows[0].token_hash


def test_consume_reset_changes_password_revokes_sessions_and_preserves_mfa(db):
    user, account = _account_user(db)
    db.add(
        AuthSession(
            user_id=user.id,
            account_id=account.id,
            token_hash="a" * 64,
            role="user",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    db.add(
        MfaChallenge(
            token_hash="b" * 64,
            subject_type="account",
            subject_id=account.id,
            account_id=account.id,
            user_id=user.id,
            role="user",
            username=account.username,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    raw = create_password_reset_token(db, user.id)

    changed = consume_password_reset(db, raw, "new-strong-pass-456")
    db.commit()

    assert changed.id == account.id
    assert verify_password("new-strong-pass-456", changed.password_hash)
    assert db.query(AuthSession).filter_by(account_id=account.id).count() == 0
    assert db.query(MfaChallenge).filter_by(account_id=account.id).count() == 0
    assert db.query(PasswordResetToken).count() == 0


@pytest.mark.parametrize("case", ["expired", "used", "blocked"])
def test_reset_rejects_invalid_token_before_password_hash(db, monkeypatch, case):
    user, _ = _account_user(db)
    raw = create_password_reset_token(db, user.id)
    row = db.query(PasswordResetToken).one()
    if case == "expired":
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif case == "used":
        db.delete(row)
    else:
        user.is_blocked = True
    db.commit()
    called = False

    def fail_hash(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("无效令牌不应执行 scrypt")

    monkeypatch.setattr("wecom_ai_gateway.password_reset.hash_password", fail_hash)
    with pytest.raises(PasswordResetError):
        consume_password_reset(db, raw, "new-strong-pass-456")
    assert called is False


def test_account_reset_command_returns_fragment_link_and_no_activation_token(db, monkeypatch):
    from wecom_ai_gateway.config import settings

    monkeypatch.setattr(settings, "public_base_url", "https://wxkf.example.com/")
    user, _ = _account_user(db)
    row = Message(
        user_id=user.id,
        channel="wecom_kf",
        channel_instance_id="wk1",
        external_message_id="reset-command",
        direction=MessageDirection.inbound,
        message_type="text",
        content="/account reset",
        status=MessageStatus.processing,
    )
    db.add(row)
    db.commit()

    reply = _handle_account_command(db, row)

    match = re.search(r"/#reset=([A-Za-z0-9_-]+)", reply or "")
    assert match
    raw = match.group(1)
    stored = db.query(PasswordResetToken).one()
    assert stored.token_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in stored.token_hash


@pytest.mark.anyio
async def test_account_reset_is_delivered_but_raw_token_is_redacted_from_history(db, monkeypatch):
    from wecom_ai_gateway.config import settings
    from wecom_ai_gateway.models import Conversation, UserSettings

    monkeypatch.setattr(settings, "public_base_url", "https://wxkf.example.com")
    user, _ = _account_user(db)
    db.add(UserSettings(user_id=user.id))
    conversation = Conversation(user_id=user.id)
    identity = ChannelIdentity(
        user_id=user.id,
        channel="wecom_kf",
        account_id="wk-reset",
        external_id_hash="c" * 64,
        external_id_encrypted=encrypt_secret("external-reset-user"),
    )
    db.add_all([conversation, identity])
    db.flush()
    row = Message(
        user_id=user.id,
        conversation_id=conversation.id,
        channel="wecom_kf",
        channel_instance_id="wk-reset",
        external_message_id="reset-process",
        direction=MessageDirection.inbound,
        message_type="text",
        content="/account reset",
        status=MessageStatus.queued,
        metadata_json={"open_kfid": "wk-reset"},
    )
    db.add(row)
    db.commit()
    send = AsyncMock(return_value="sent-reset-link")

    with patch("wecom_ai_gateway.services.client.send_text", send):
        await process_message(row.id)

    send.assert_awaited_once()
    assert "#reset=" in send.await_args.args[2]
    db.expire_all()
    outbound = db.query(Message).filter_by(external_message_id="sent-reset-link").one()
    assert outbound.content == "[密码重置链接已发送]"
    assert "#reset=" not in outbound.content


def test_reset_api_requires_confirmation_and_does_not_auto_login():
    db = SessionLocal()
    user, account = _account_user(db, "reset_api_user")
    raw = create_password_reset_token(db, user.id)
    account_id = account.id
    db.close()

    mismatch = client.post(
        "/api/auth/reset-password",
        json={
            "reset_token": raw,
            "password": "new-strong-pass-456",
            "confirm_password": "different-pass-789",
        },
    )
    assert mismatch.status_code == 400

    response = client.post(
        "/api/auth/reset-password",
        json={
            "reset_token": raw,
            "password": "new-strong-pass-456",
            "confirm_password": "new-strong-pass-456",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "username": "reset_api_user", "mfa_preserved": False}
    assert "token" not in response.json()
    db = SessionLocal()
    assert db.get(Account, account_id).user_id == user.id
    assert db.query(AuthSession).count() == 0
    db.close()
