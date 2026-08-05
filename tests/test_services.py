from unittest.mock import AsyncMock, patch

import pytest

from wecom_ai_gateway.channels import ChannelMessage
from wecom_ai_gateway.models import (
    ChannelIdentity,
    Conversation,
    Message,
    MessageStatus,
    OutboxTask,
    User,
    UserSettings,
)
from wecom_ai_gateway.providers import CompletionResult
from wecom_ai_gateway.security import encrypt_secret
from wecom_ai_gateway.services import _complete_ai, ingest, ingest_channel_message, process_message


def inbound_item(msgid: str, user: str = "wmSyntheticUser") -> dict:
    return {
        "msgid": msgid,
        "open_kfid": "wkSyntheticAccount",
        "external_userid": user,
        "msgtype": "text",
        "origin": 3,
        "text": {"content": "你好"},
    }


def test_ingest_is_idempotent_and_isolated(db):
    with patch("wecom_ai_gateway.services.encrypt_secret", side_effect=lambda value: f"encrypted:{value}"):
        ingest(db, inbound_item("msg-1", "user-a"))
        ingest(db, inbound_item("msg-1", "user-a"))
        ingest(db, inbound_item("msg-2", "user-b"))
        db.commit()
    rows = db.query(Message).all()
    assert len(rows) == 2
    assert len({row.user_id for row in rows}) == 2
    assert all(row.status == MessageStatus.queued for row in rows)


def test_generic_channel_ingest_is_idempotent_and_enqueues_message_task(db):
    incoming = ChannelMessage(
        channel="wechat_clawbot",
        instance_id="instance-1",
        sender_id="wechat-user-1",
        external_message_id="clawbot-message-1",
        content="你好",
        raw={"source": "bridge"},
    )
    first = ingest_channel_message(db, incoming)
    second = ingest_channel_message(db, incoming)
    db.commit()

    assert first is not None
    assert second is None
    assert first.channel == "wechat_clawbot"
    assert first.status == MessageStatus.queued
    assert first.metadata_json["instance_id"] == "instance-1"
    assert db.query(ChannelIdentity).filter_by(channel="wechat_clawbot", account_id="instance-1").count() == 1
    assert db.query(OutboxTask).filter_by(dedupe_key=f"message:{first.id}").count() == 1
    assert first.user_id != ""


def test_non_customer_message_is_not_queued(db):
    item = inbound_item("event-1")
    item.update({"origin": 4, "msgtype": "event", "text": None})
    with patch("wecom_ai_gateway.services.encrypt_secret", side_effect=lambda value: f"encrypted:{value}"):
        ingest(db, item)
        db.commit()
    assert db.query(Message).one().status == MessageStatus.ignored


@pytest.mark.anyio
async def test_unconfigured_model_returns_maintenance_message(db):
    user = User()
    db.add(user)
    db.flush()
    user_settings = UserSettings(user_id=user.id)
    conversation = Conversation(user_id=user.id)
    db.add_all([user_settings, conversation])
    db.flush()
    row = Message(
        user_id=user.id,
        conversation_id=conversation.id,
        channel="wecom_kf",
        external_message_id="msg-no-api",
        direction="inbound",
        message_type="text",
        content="你好",
        status=MessageStatus.processing,
    )
    db.add(row)
    db.flush()
    with patch("wecom_ai_gateway.services.settings.openai_compatible_api_key", ""):
        answer = await _complete_ai(db, row, conversation, user_settings)
    assert "配置中" in answer


@pytest.mark.anyio
async def test_empty_model_reply_is_retried_instead_of_sending_fallback(db):
    """长推理期间的空响应不能被伪装成一条成功客服回复。"""
    user = User()
    db.add(user)
    db.flush()
    user_settings = UserSettings(user_id=user.id)
    conversation = Conversation(user_id=user.id)
    identity = ChannelIdentity(
        user_id=user.id,
        channel="wecom_kf",
        account_id="wkSyntheticAccount",
        external_id_hash="a" * 64,
        external_id_encrypted=encrypt_secret("external-user"),
    )
    db.add_all([user_settings, conversation, identity])
    db.flush()
    row = Message(
        user_id=user.id,
        conversation_id=conversation.id,
        channel="wecom_kf",
        external_message_id="msg-empty-model-reply",
        direction="inbound",
        message_type="text",
        content="请认真想一想",
        status=MessageStatus.queued,
        metadata_json={"open_kfid": "wkSyntheticAccount"},
    )
    db.add_all([user_settings, conversation, identity, row])
    db.commit()

    provider = AsyncMock()
    provider.complete.return_value = CompletionResult(content="")
    send = AsyncMock()
    with (
        patch("wecom_ai_gateway.services.resolve_provider", return_value=("openai-compatible", "https://example.invalid/v1", "test-key")),
        patch("wecom_ai_gateway.services.provider_for", return_value=provider),
        patch("wecom_ai_gateway.services.client.send_text", send),
        pytest.raises(RuntimeError, match="未生成可发送"),
    ):
        await process_message(row.id)

    send.assert_not_awaited()
    db.expire_all()
    failed = db.get(Message, row.id)
    assert failed.status == MessageStatus.failed
    assert "暂时没有生成可发送的内容" not in (failed.error or "")
