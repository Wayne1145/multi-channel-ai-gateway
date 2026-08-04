from unittest.mock import patch

from wecom_ai_gateway.models import Message, MessageStatus
from wecom_ai_gateway.services import ingest


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
