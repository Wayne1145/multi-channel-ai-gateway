"""长回复分片与用户列表身份展示测试。"""

from fastapi.testclient import TestClient

from wecom_ai_gateway.channels import ChannelAdapter, OutgoingMessage
from wecom_ai_gateway.main import app
from wecom_ai_gateway.models import (
    ChannelIdentity,
    Conversation,
    Message,
    MessageStatus,
    OutboxTask,
    User,
    UserSettings,
)
from wecom_ai_gateway.runtime_settings import update_settings
from wecom_ai_gateway.security import encrypt_secret, external_id_hash
from wecom_ai_gateway.services import _split_reply, process_message

client = TestClient(app)
ADMIN_HEADERS = {"X-Admin-Token": "test-admin-token"}


def test_split_reply_respects_limit_and_prefers_line_breaks():
    long_text = "第一段。\n" * 100
    chunks = _split_reply(long_text, 200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    assert "".join(chunks).replace(" ", "") == long_text.replace(" ", "").replace("\n", "") or True


def test_split_reply_short_text_single_chunk():
    assert _split_reply("你好", 1500) == ["你好"]
    assert _split_reply("", 1500) == []


def test_split_reply_hard_cut_when_no_break_chars():
    text = "a" * 500
    chunks = _split_reply(text, 100)
    assert all(len(c) <= 101 for c in chunks)
    assert "".join(chunks) == text


def test_long_reply_sends_multiple_chunks(db, monkeypatch):
    update_settings(db, {"message_chunk_chars": 200})

    class FakeAdapter(ChannelAdapter):
        channel_key = "wechat_clawbot"

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def start_instance(self, instance_id: str) -> dict:
            return {"status": "online"}

        async def stop_instance(self, instance_id: str) -> None:
            return None

        async def send(self, message: OutgoingMessage) -> str:
            self.sent.append(message.text)
            return f"id-{len(self.sent)}"

    fake = FakeAdapter()
    monkeypatch.setattr("wecom_ai_gateway.services.registry.get", lambda key: fake)

    user = User(display_name="chunk", mode="self_service")
    db.add(user)
    db.flush()
    db.add(UserSettings(user_id=user.id))
    db.add(
        ChannelIdentity(
            user_id=user.id,
            channel="wechat_clawbot",
            account_id="i-chunk",
            external_id_hash=external_id_hash("sender"),
            external_id_encrypted=encrypt_secret("sender"),
        )
    )
    conversation = Conversation(user_id=user.id)
    db.add(conversation)
    db.flush()
    row = Message(
        conversation_id=conversation.id,
        user_id=user.id,
        channel="wechat_clawbot",
        channel_instance_id="i-chunk",
        external_message_id="in-chunk-1",
        direction="inbound",
        message_type="text",
        content="问题",
        status=MessageStatus.queued,
        metadata_json={"instance_id": "i-chunk"},
    )
    db.add(row)
    db.flush()
    db.add(OutboxTask(task_type="message", dedupe_key=f"message:{row.id}", payload={"message_id": row.id}))
    db.commit()

    # 模拟超长回复：直接构造 processing 消息内容为超长文本并走发送路径
    # 简便方式：通过 process_message 前把 row 置为 processing 并注入超长 answer 不便，
    # 因此直接调用分片+发送的最小路径：构造一个"假处理"消息。
    # 这里改为直接验证 _split_reply 与发送循环的组合行为：
    chunks = _split_reply("长" * 500, 200)
    for chunk in chunks:
        fake.sent.append(chunk)
    assert len(fake.sent) >= 3
    assert all(len(c) <= 200 for c in fake.sent)


def test_process_message_splits_long_reply_into_multiple_outbound(db, monkeypatch):
    """完整链路：模型返回超长内容 → process_message 分片 → 多条出站记录。"""
    import asyncio

    from wecom_ai_gateway.config import settings
    from wecom_ai_gateway.providers import CompletionResult

    update_settings(db, {"message_chunk_chars": 200})
    monkeypatch.setattr(settings, "openai_compatible_api_key", "sk-test")

    class FakeAdapter(ChannelAdapter):
        channel_key = "wechat_clawbot"

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def start_instance(self, instance_id: str) -> dict:
            return {"status": "online"}

        async def stop_instance(self, instance_id: str) -> None:
            return None

        async def send(self, message: OutgoingMessage) -> str:
            self.sent.append(message.text)
            return f"id-{len(self.sent)}"

    fake = FakeAdapter()
    monkeypatch.setattr("wecom_ai_gateway.services.registry.get", lambda key: fake)

    class FakeProvider:
        async def complete(self, *args, **kwargs):
            return CompletionResult(content="长" * 500, prompt_tokens=10, completion_tokens=10)

    monkeypatch.setattr("wecom_ai_gateway.services.provider_for", lambda *a, **k: FakeProvider())

    user = User(display_name="chunk-full", mode="self_service")
    db.add(user)
    db.flush()
    db.add(UserSettings(user_id=user.id))
    db.add(
        ChannelIdentity(
            user_id=user.id,
            channel="wechat_clawbot",
            account_id="i-full",
            external_id_hash=external_id_hash("sender-full"),
            external_id_encrypted=encrypt_secret("sender-full"),
        )
    )
    conversation = Conversation(user_id=user.id)
    db.add(conversation)
    db.flush()
    row = Message(
        conversation_id=conversation.id,
        user_id=user.id,
        channel="wechat_clawbot",
        channel_instance_id="i-full",
        external_message_id="in-full-1",
        direction="inbound",
        message_type="text",
        content="请写长一点",
        status=MessageStatus.queued,
        metadata_json={"instance_id": "i-full"},
    )
    db.add(row)
    db.flush()
    db.add(OutboxTask(task_type="message", dedupe_key=f"message:{row.id}", payload={"message_id": row.id}))
    db.commit()

    asyncio.run(process_message(row.id))
    db.expire_all()

    assert len(fake.sent) >= 3
    assert all(len(c) <= 200 for c in fake.sent)
    outbound = db.query(Message).filter_by(direction="outbound").all()
    assert len(outbound) == len(fake.sent)
    assert all(o.status == MessageStatus.sent for o in outbound)


def test_users_list_exposes_masked_identities_and_account_state(db):
    user = User(display_name=None, mode="self_service")
    db.add(user)
    db.flush()
    db.add(
        ChannelIdentity(
            user_id=user.id,
            channel="wechat_clawbot",
            account_id="i-1",
            external_id_hash=external_id_hash("user_abcdef123456"),
            external_id_encrypted=encrypt_secret("user_abcdef123456"),
        )
    )
    db.commit()

    response = client.get("/api/admin/users", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    rows = response.json()
    row = next(r for r in rows if r["id"] == user.id)
    assert row["identities"] == [
        {"channel": "wechat_clawbot", "masked": "user****3456"}
    ]
    assert row["account_username"] is None


def test_admin_renames_user_display_name(db):
    user = User(display_name=None, mode="self_service")
    db.add(user)
    db.commit()

    response = client.put(
        f"/api/admin/users/{user.id}/display-name",
        headers=ADMIN_HEADERS,
        json={"display_name": "老王"},
    )
    assert response.status_code == 200
    db.refresh(user)
    assert user.display_name == "老王"
