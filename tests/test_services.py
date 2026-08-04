from unittest.mock import patch

import pytest

from wecom_ai_gateway.models import Conversation, Message, MessageStatus, User, UserSettings
from wecom_ai_gateway.services import _complete_ai, ingest


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
