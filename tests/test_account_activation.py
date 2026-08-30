"""微信用户后台账号自助激活测试。"""

import hashlib
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from wecom_ai_gateway.account_activation import (
    ActivationError,
    activate_account,
    create_activation_token,
)
from wecom_ai_gateway.db import SessionLocal
from wecom_ai_gateway.main import app
from wecom_ai_gateway.models import (
    Account,
    AccountActivationToken,
    AuthSession,
    ChannelIdentity,
    Message,
    MessageDirection,
    MessageStatus,
    User,
)
from wecom_ai_gateway.security import encrypt_secret, verify_password
from wecom_ai_gateway.services import _handle_account_command, process_message

client = TestClient(app)


def _user(db, name: str = "微信用户") -> User:
    user = User(display_name=name, mode="self_service")
    db.add(user)
    db.commit()
    return user


def test_activation_token_is_high_entropy_hashed_and_replaces_old_token(db):
    user = _user(db)

    first = create_activation_token(db, user.id)
    second = create_activation_token(db, user.id)

    assert len(first) >= 40
    assert first != second
    rows = db.query(AccountActivationToken).filter_by(user_id=user.id).all()
    assert len(rows) == 1
    assert rows[0].token_hash == hashlib.sha256(second.encode()).hexdigest()
    assert first not in rows[0].token_hash
    assert second not in rows[0].token_hash


def test_activate_account_creates_credentials_for_original_user_and_consumes_token(db):
    user = _user(db)
    raw_token = create_activation_token(db, user.id)

    account = activate_account(db, raw_token, "wechat_user", "strong-pass-123")

    assert account.user_id == user.id
    assert account.username == "wechat_user"
    assert verify_password("strong-pass-123", account.password_hash)
    assert db.query(AccountActivationToken).count() == 0


@pytest.mark.parametrize("case", ["expired", "used", "blocked", "existing_account"])
def test_activation_rejects_invalid_or_ineligible_token(db, case):
    user = _user(db)
    raw_token = create_activation_token(db, user.id)
    row = db.query(AccountActivationToken).one()
    if case == "expired":
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif case == "used":
        db.delete(row)
    elif case == "blocked":
        user.is_blocked = True
    else:
        db.add(
            Account(
                user_id=user.id,
                username="existing",
                password_hash="not-used",
                role="user",
            )
        )
    db.commit()

    with pytest.raises(ActivationError):
        activate_account(db, raw_token, "new_user", "strong-pass-123")

    assert db.query(AuthSession).count() == 0


def test_account_command_issues_fragment_link_without_persisting_raw_token(db, monkeypatch):
    from wecom_ai_gateway.config import settings

    monkeypatch.setattr(settings, "public_base_url", "https://wxkf.example.com/")
    user = _user(db)
    row = Message(
        user_id=user.id,
        channel="wecom_kf",
        channel_instance_id="wk1",
        external_message_id="account-command-1",
        direction=MessageDirection.inbound,
        message_type="text",
        content="/account",
        status=MessageStatus.processing,
    )
    db.add(row)
    db.commit()

    reply = _handle_account_command(db, row)

    assert reply is not None
    match = re.search(r"https://wxkf\.example\.com/#activate=([A-Za-z0-9_-]+)", reply)
    assert match
    raw_token = match.group(1)
    stored = db.query(AccountActivationToken).filter_by(user_id=user.id).one()
    assert stored.token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
    assert raw_token not in stored.token_hash


@pytest.mark.anyio
async def test_delivered_activation_reply_redacts_raw_token_from_message_history(db, monkeypatch):
    from wecom_ai_gateway.config import settings
    from wecom_ai_gateway.models import Conversation
    from wecom_ai_gateway.services import _deliver_reply

    monkeypatch.setattr(settings, "public_base_url", "https://wxkf.example.com")
    user = _user(db)
    conversation = Conversation(user_id=user.id)
    identity = ChannelIdentity(
        user_id=user.id,
        channel="wecom_kf",
        account_id="wk1",
        external_id_hash="b" * 64,
        external_id_encrypted=encrypt_secret("external-redaction-user"),
    )
    db.add_all([conversation, identity])
    db.flush()
    row = Message(
        user_id=user.id,
        conversation_id=conversation.id,
        channel="wecom_kf",
        channel_instance_id="wk1",
        external_message_id="account-command-redaction",
        direction=MessageDirection.inbound,
        message_type="text",
        content="/account",
        status=MessageStatus.processing,
        metadata_json={"open_kfid": "wk1"},
    )
    db.add(row)
    db.commit()
    reply = _handle_account_command(db, row)
    raw_token = reply.split("#activate=", 1)[1].split()[0]

    send = AsyncMock(return_value="redacted-link-reply")
    with patch("wecom_ai_gateway.services.client.send_text", send):
        await _deliver_reply(
            db,
            conversation,
            row,
            reply,
            stored_content="[账号激活链接已发送]",
        )

    check = SessionLocal()
    outbound = check.query(Message).filter_by(direction=MessageDirection.outbound).one()
    assert outbound.content == "[账号激活链接已发送]"
    assert raw_token not in outbound.content
    check.close()


def test_account_command_for_existing_account_returns_login_instead_of_token(db, monkeypatch):
    from wecom_ai_gateway.config import settings

    monkeypatch.setattr(settings, "public_base_url", "https://wxkf.example.com")
    user = _user(db)
    db.add(
        Account(
            user_id=user.id,
            username="already_active",
            password_hash="not-used",
            role="user",
        )
    )
    row = Message(
        user_id=user.id,
        channel="wecom_kf",
        channel_instance_id="wk1",
        external_message_id="account-command-2",
        direction=MessageDirection.inbound,
        message_type="text",
        content="/account",
        status=MessageStatus.processing,
    )
    db.add(row)
    db.commit()

    reply = _handle_account_command(db, row)

    assert "already_active" in reply
    assert "https://wxkf.example.com" in reply
    assert "#activate=" not in reply
    assert db.query(AccountActivationToken).count() == 0


@pytest.mark.anyio
async def test_account_command_is_actually_sent_to_wecom_and_history_is_redacted(db, monkeypatch):
    from wecom_ai_gateway.config import settings
    from wecom_ai_gateway.models import Conversation, UserSettings

    monkeypatch.setattr(settings, "public_base_url", "https://wxkf.example.com")
    user = _user(db)
    db.add(UserSettings(user_id=user.id))
    conversation = Conversation(user_id=user.id)
    identity = ChannelIdentity(
        user_id=user.id,
        channel="wecom_kf",
        account_id="wk-account",
        external_id_hash="a" * 64,
        external_id_encrypted=encrypt_secret("external-user"),
    )
    db.add_all([conversation, identity])
    db.flush()
    row = Message(
        user_id=user.id,
        conversation_id=conversation.id,
        channel="wecom_kf",
        channel_instance_id="wk-account",
        external_message_id="account-process-1",
        direction=MessageDirection.inbound,
        message_type="text",
        content="/account",
        status=MessageStatus.queued,
        metadata_json={"open_kfid": "wk-account"},
    )
    db.add(row)
    db.commit()
    send = AsyncMock(return_value="sent-account-link")

    with patch("wecom_ai_gateway.services.client.send_text", send):
        await process_message(row.id)

    send.assert_awaited_once()
    assert send.await_args.args[:2] == ("wk-account", "external-user")
    assert "#activate=" in send.await_args.args[2]
    db.expire_all()
    outbound = db.query(Message).filter_by(external_message_id="sent-account-link").one()
    assert outbound.content == "[账号激活链接已发送]"
    assert "#activate=" not in outbound.content


def test_activation_api_creates_original_user_account_and_logs_in():
    db = SessionLocal()
    user = _user(db)
    user_id = user.id
    raw_token = create_activation_token(db, user.id)
    db.close()

    response = client.post(
        "/api/auth/activate",
        json={
            "activation_token": raw_token,
            "username": "wechat_owner",
            "password": "strong-pass-123",
            "confirm_password": "strong-pass-123",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "user"
    assert body["user_id"] == user_id
    assert body["username"] == "wechat_owner"
    assert len(body["token"]) >= 32
    assert raw_token not in response.text
    assert client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"}
    ).status_code == 200


@pytest.mark.parametrize(
    ("payload_patch", "expected_status"),
    [
        ({"confirm_password": "different-pass-456"}, 400),
        ({"username": "ab"}, 400),
        ({"username": "admin"}, 409),
    ],
)
def test_activation_api_rejects_invalid_credentials_without_consuming_token(
    payload_patch, expected_status
):
    db = SessionLocal()
    user = _user(db)
    raw_token = create_activation_token(db, user.id)
    db.close()
    payload = {
        "activation_token": raw_token,
        "username": "valid_user",
        "password": "strong-pass-123",
        "confirm_password": "strong-pass-123",
    }
    payload.update(payload_patch)

    response = client.post("/api/auth/activate", json=payload)

    assert response.status_code == expected_status
    db = SessionLocal()
    assert db.query(AccountActivationToken).count() == 1
    assert db.query(Account).count() == 0
    db.close()


def test_activation_api_rejects_duplicate_username_without_consuming_token():
    db = SessionLocal()
    first = _user(db, "已有用户")
    db.add(
        Account(
            user_id=first.id,
            username="taken_name",
            password_hash="not-used",
            role="user",
        )
    )
    second = _user(db, "待激活用户")
    raw_token = create_activation_token(db, second.id)
    db.close()

    response = client.post(
        "/api/auth/activate",
        json={
            "activation_token": raw_token,
            "username": "taken_name",
            "password": "strong-pass-123",
            "confirm_password": "strong-pass-123",
        },
    )

    assert response.status_code == 409
    db = SessionLocal()
    assert db.query(AccountActivationToken).filter_by(user_id=second.id).count() == 1
    db.close()
