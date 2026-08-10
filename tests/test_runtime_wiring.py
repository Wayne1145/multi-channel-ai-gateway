"""运行时设置接入关键路径的回归测试：配额/长度/数量上限/密码/锁定/维护。"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from wecom_ai_gateway.channels import ChannelMessage
from wecom_ai_gateway.commands import execute
from wecom_ai_gateway.db import SessionLocal
from wecom_ai_gateway.main import app
from wecom_ai_gateway.models import (
    Account,
    AuthSession,
    CharacterCard,
    Memory,
    UsageRecord,
    User,
    UserSettings,
)
from wecom_ai_gateway.runtime_settings import update_settings
from wecom_ai_gateway.security import hash_password
from wecom_ai_gateway.services import ingest_channel_message, quota_status

client = TestClient(app)


def _user(db, name="alice") -> User:
    user = User(display_name=name, mode="self_service")
    db.add(user)
    db.flush()
    db.add(UserSettings(user_id=user.id))
    db.commit()
    return user


def _run(db, user_id: str, text: str):
    return execute(db, user_id, text)


# ---------- 配额 ----------

def test_quota_status_uses_runtime_value(db):
    user = _user(db)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    db.add(UsageRecord(user_id=user.id, provider="p", model="m", prompt_tokens=8000, completion_tokens=0, created_at=today))
    db.commit()
    update_settings(db, {"user_daily_token_quota": 5000, "daily_quota_enabled": True})

    status = quota_status(db, user.id, None)
    assert status["quota"] == 5000
    assert status["used"] == 8000
    assert status["exceeded"] is True


def test_quota_status_respects_disabled_switch(db):
    user = _user(db)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    db.add(UsageRecord(user_id=user.id, provider="p", model="m", prompt_tokens=99999, completion_tokens=0, created_at=today))
    db.commit()
    update_settings(db, {"daily_quota_enabled": False})

    status = quota_status(db, user.id, None)
    assert status["exceeded"] is False


# ---------- 消息长度 ----------

def test_ingest_truncates_overlong_text(db):
    update_settings(db, {"message_max_chars": 100})
    row = ingest_channel_message(
        db,
        ChannelMessage(
            channel="wechat_clawbot", instance_id="i-1", sender_id="s-1",
            external_message_id="m-1", content="长" * 200,
        ),
    )
    assert len(row.content) == 100


# ---------- 命令上限 ----------

def test_context_limit_comes_from_runtime(db):
    user = _user(db)
    update_settings(db, {"max_context_messages": 10})
    result = _run(db, user.id, "/context 50")
    assert "超出允许范围" in result.reply


def test_card_new_respects_count_limit(db):
    user = _user(db)
    update_settings(db, {"max_cards_per_user": 1})
    assert _run(db, user.id, "/card new 第一张").handled
    result = _run(db, user.id, "/card new 第二张")
    assert "上限" in result.reply


def test_card_set_truncates_to_runtime_limit(db):
    user = _user(db)
    update_settings(db, {"card_max_chars": 500})
    _run(db, user.id, "/card new 卡片")
    _run(db, user.id, "/card set " + "内" * 1000)
    card = db.query(CharacterCard).filter_by(user_id=user.id).first()
    from wecom_ai_gateway.cards import decrypt_card_content

    assert len(decrypt_card_content(card.content_encrypted)) == 500


def test_memory_add_respects_count_and_length_limits(db):
    user = _user(db)
    update_settings(db, {"max_memories_per_user": 1, "memory_max_chars": 100})
    assert _run(db, user.id, "/memory add " + "记" * 200).handled
    row = db.query(Memory).filter_by(user_id=user.id).one()
    from wecom_ai_gateway.commands import memory_text

    assert len(memory_text(row)) == 100
    result = _run(db, user.id, "/memory add 第二条")
    assert "上限" in result.reply


def test_preset_save_respects_count_limit(db):
    user = _user(db)
    update_settings(db, {"max_presets_per_user": 1})
    assert _run(db, user.id, "/preset save 预设一").handled
    result = _run(db, user.id, "/preset save 预设二")
    assert "上限" in result.reply


# ---------- 认证 ----------

def test_register_respects_runtime_password_min_length(db):
    update_settings(db, {"allow_public_registration": True, "password_min_length": 12})
    short = client.post(
        "/api/auth/register",
        json={"username": "bob", "password": "short-pass", "display_name": "Bob"},
    )
    assert short.status_code == 400
    ok = client.post(
        "/api/auth/register",
        json={"username": "bob", "password": "long-pass-12345", "display_name": "Bob"},
    )
    assert ok.status_code == 200


def test_session_days_comes_from_runtime(db):
    update_settings(db, {"auth_session_days": 1})
    user = _user(db)
    db.add(Account(user_id=user.id, username="carol", password_hash=hash_password("strong-pass-123"), role="user"))
    db.commit()
    ok = client.post("/api/auth/login", json={"username": "carol", "password": "strong-pass-123"})
    assert ok.status_code == 200
    row = db.query(AuthSession).filter_by(user_id=user.id).one()
    delta = row.expires_at.replace(tzinfo=UTC) - datetime.now(UTC)
    assert timedelta(hours=23) < delta <= timedelta(days=1, hours=1)


class FakeRedis:
    def __init__(self):
        self.data = {}

    def incr(self, key):
        self.data[key] = self.data.get(key, 0) + 1
        return self.data[key]

    def get(self, key):
        return self.data.get(key)

    def expire(self, key, seconds):
        return True

    def delete(self, key):
        self.data.pop(key, None)
        return 1


def test_login_locks_after_runtime_threshold(monkeypatch):
    import wecom_ai_gateway.auth as auth_module

    fake = FakeRedis()
    monkeypatch.setattr(auth_module, "redis_client", lambda: fake)
    db = SessionLocal()
    user = User(display_name="locked", mode="self_service")
    db.add(user)
    db.flush()
    db.add(Account(user_id=user.id, username="locked", password_hash=hash_password("strong-pass-123"), role="user"))
    update_settings(db, {"login_max_attempts": 3, "login_lock_minutes": 10})
    db.commit()
    db.close()

    for _ in range(3):
        bad = client.post("/api/auth/login", json={"username": "locked", "password": "wrong-password"})
        assert bad.status_code == 401
    locked = client.post("/api/auth/login", json={"username": "locked", "password": "strong-pass-123"})
    assert locked.status_code == 429


# ---------- 渠道与维护 ----------

def test_user_instance_creation_blocked_by_runtime(db):
    user = _user(db)
    db.add(Account(user_id=user.id, username="dave", password_hash=hash_password("strong-pass-123"), role="user"))
    db.commit()
    update_settings(db, {"allow_user_clawbot_instances": False})
    token = client.post("/api/auth/login", json={"username": "dave", "password": "strong-pass-123"}).json()["token"]
    response = client.post(
        "/api/me/channel-instances",
        headers={"Authorization": f"Bearer {token}"},
        json={"instance_name": "我的微信"},
    )
    assert response.status_code == 403


def test_maintenance_mode_replies_with_notice_instead_of_ai(db, monkeypatch):
    """维护模式下消息正常入库，但 process 时回复维护提示而不调用模型。"""
    import asyncio

    from wecom_ai_gateway.channels import ChannelAdapter, OutgoingMessage
    from wecom_ai_gateway.models import Message, MessageStatus, OutboxStatus, OutboxTask
    from wecom_ai_gateway.services import process_message
    from wecom_ai_gateway.tasks import add_message_task

    class FakeAdapter(ChannelAdapter):
        channel_key = "wechat_clawbot"

        async def start_instance(self, instance_id: str) -> dict:
            return {"status": "online"}

        async def stop_instance(self, instance_id: str) -> None:
            return None

        async def send(self, message: OutgoingMessage) -> str:
            return "fake-outbound-id"

    monkeypatch.setattr(
        "wecom_ai_gateway.services.registry.get", lambda key: FakeAdapter()
    )

    update_settings(db, {"maintenance_mode": True, "maintenance_message": "系统维护中，请稍后再试。"})
    user = User(display_name="maint", mode="self_service")
    db.add(user)
    db.flush()
    db.add(UserSettings(user_id=user.id))
    from wecom_ai_gateway.models import ChannelIdentity, Conversation
    from wecom_ai_gateway.security import encrypt_secret, external_id_hash

    conversation = Conversation(user_id=user.id)
    db.add(conversation)
    db.flush()
    db.add(
        ChannelIdentity(
            user_id=user.id,
            channel="wechat_clawbot",
            account_id="i-maint",
            external_id_hash=external_id_hash("sender-maint"),
            external_id_encrypted=encrypt_secret("sender-maint"),
        )
    )
    row = Message(
        conversation_id=conversation.id,
        user_id=user.id,
        channel="wechat_clawbot",
        channel_instance_id="i-maint",
        external_message_id="m-maint-2",
        direction="inbound",
        message_type="text",
        content="你好",
        status=MessageStatus.queued,
        metadata_json={"instance_id": "i-maint"},
    )
    db.add(row)
    db.flush()
    add_message_task(db, row.id)
    db.commit()

    asyncio.run(process_message(row.id))
    db.expire_all()

    refreshed = db.get(Message, row.id)
    assert refreshed.status == MessageStatus.sent
    outbound = db.query(Message).filter_by(direction="outbound").order_by(Message.created_at.desc()).first()
    assert outbound is not None
    assert outbound.content == "系统维护中，请稍后再试。"
    task = db.query(OutboxTask).filter_by(dedupe_key=f"message:{row.id}").first()
    # 任务完成由 Worker 的 drain 循环负责；此处只需确认没有进入死信
    assert task.status != OutboxStatus.dead


def test_auth_config_exposes_announcement_and_maintenance(db):
    update_settings(db, {"announcement": "今晚维护", "maintenance_mode": True})
    response = client.get("/api/auth/config")
    assert response.status_code == 200
    body = response.json()
    assert body["announcement"] == "今晚维护"
    assert body["maintenance_mode"] is True
