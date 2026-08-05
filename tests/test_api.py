import pytest
from fastapi.testclient import TestClient

from wecom_ai_gateway.db import SessionLocal
from wecom_ai_gateway.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_admin_auth():
    assert client.get("/api/admin/stats").status_code == 401
    headers = {"X-Admin-Token": "test-admin-token"}
    assert client.get("/api/admin/stats", headers=headers).status_code == 200
    assert client.get("/api/admin/tasks/dead", headers=headers).status_code == 200
    assert client.post("/api/admin/tasks/missing/replay", headers=headers).status_code == 409


def test_usage_trend():
    from datetime import UTC, datetime

    from wecom_ai_gateway.db import SessionLocal
    from wecom_ai_gateway.models import UsageRecord, User

    headers = {"X-Admin-Token": "test-admin-token"}
    db = SessionLocal()
    db.add(User(id="u1"))
    db.flush()
    db.add(UsageRecord(user_id="u1", provider="openai-compatible", model="deepseek-chat", prompt_tokens=100, completion_tokens=50))
    db.add(UsageRecord(user_id="u1", provider="openai-compatible", model="deepseek-chat", prompt_tokens=30, completion_tokens=20))
    db.commit()
    db.close()

    r = client.get("/api/admin/usage/trend?days=7", headers=headers)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 7
    today = datetime.now(UTC).date().isoformat()
    today_row = next(row for row in rows if row["date"] == today)
    assert today_row["tokens"] == 200
    assert client.get("/api/admin/usage/trend?days=999", headers=headers).status_code == 200


def test_mode_api():
    from wecom_ai_gateway.db import SessionLocal
    from wecom_ai_gateway.models import User

    headers = {"X-Admin-Token": "test-admin-token"}
    assert client.get("/api/admin/mode").status_code == 401
    r = client.get("/api/admin/mode", headers=headers)
    assert r.status_code == 200 and r.json()["platform_mode"] == "self_service"

    assert client.post("/api/admin/mode", headers=headers, json={"mode": "managed"}).status_code == 200
    assert client.get("/api/admin/mode", headers=headers).json()["configured_mode"] == "managed"
    assert client.post("/api/admin/mode", headers=headers, json={"mode": None}).status_code == 200
    assert client.get("/api/admin/mode", headers=headers).json()["configured_mode"] is None

    db = SessionLocal()
    u = User()
    db.add(u)
    db.commit()
    uid = u.id
    db.close()
    r = client.post(f"/api/admin/users/{uid}/mode", headers=headers, json={"mode": "managed"})
    assert r.status_code == 200 and r.json()["mode"] == "managed"
    r = client.get(f"/api/admin/users/{uid}/mode", headers=headers)
    assert r.json()["effective_mode"] == "managed"
    assert client.post(f"/api/admin/users/{uid}/mode", headers=headers, json={"mode": None}).status_code == 200
    assert client.post("/api/admin/users/nonexistent/mode", headers=headers, json={"mode": "managed"}).status_code == 404


def test_policy_api():
    from wecom_ai_gateway.db import SessionLocal
    from wecom_ai_gateway.models import User

    headers = {"X-Admin-Token": "test-admin-token"}
    db = SessionLocal()
    u = User()
    db.add(u)
    db.commit()
    uid = u.id
    db.close()

    r = client.post(
        f"/api/admin/users/{uid}/policies",
        headers=headers,
        json={"command": "card", "allowed": False, "silent_block": True, "blocked_strategy": "ignore"},
    )
    assert r.status_code == 200
    rows = client.get(f"/api/admin/users/{uid}/policies", headers=headers).json()
    assert any(p["command"] == "card" and p["silent_block"] for p in rows)
    # 平台级策略
    r = client.post(
        "/api/admin/policies",
        headers=headers,
        json={"command": "preset", "allowed": False},
    )
    assert r.status_code == 200
    platform_rows = client.get(f"/api/admin/users/{uid}/policies", headers=headers).json()
    assert any(p["user_id"] is None and p["command"] == "preset" for p in platform_rows)
    # 删除策略（用户级与平台级）
    user_pid = next(p["id"] for p in rows if p["command"] == "card")
    assert client.delete(f"/api/admin/policies/{user_pid}", headers=headers).status_code == 200
    pid = next(p["id"] for p in platform_rows if p["command"] == "preset")
    assert client.delete(f"/api/admin/policies/{pid}", headers=headers).status_code == 200
    assert client.get(f"/api/admin/users/{uid}/policies", headers=headers).json() == []


def test_user_cards_metadata_api():
    from wecom_ai_gateway.db import SessionLocal
    from wecom_ai_gateway.models import CharacterCard, User
    from wecom_ai_gateway.security import encrypt_secret

    headers = {"X-Admin-Token": "test-admin-token"}
    db = SessionLocal()
    u = User()
    db.add(u)
    db.commit()
    uid = u.id
    db.add(CharacterCard(user_id=uid, name="秘密卡", format="soul_md", content_encrypted=encrypt_secret("秘密内容"), active=True))
    db.commit()
    db.close()

    rows = client.get(f"/api/admin/users/{uid}/cards", headers=headers).json()
    assert len(rows) == 1
    assert rows[0]["name"] == "秘密卡"
    # 管理端绝不返回解密后的内容字段
    assert "content" not in rows[0] and "content_encrypted" not in rows[0]
    assert client.get("/api/admin/users/nonexistent/cards", headers=headers).status_code == 404


def test_channel_instance_api_does_not_expose_login_secrets(monkeypatch):
    from wecom_ai_gateway.clawbot import ClawBotAdapter
    from wecom_ai_gateway.main import registry
    from wecom_ai_gateway.models import ChannelInstance, User

    headers = {"X-Admin-Token": "test-admin-token"}
    assert client.get("/api/admin/channel-instances").status_code == 401

    db = SessionLocal()
    owner = User()
    db.add(owner)
    db.commit()
    owner_id = owner.id
    db.close()

    created = client.post(
        "/api/admin/channel-instances",
        headers=headers,
        json={
            "channel": "wechat_clawbot",
            "instance_name": "测试微信号",
            "owner_user_id": owner_id,
            "config": {"label": "仅公开配置"},
        },
    )
    assert created.status_code == 200
    instance_id = created.json()["id"]

    db = SessionLocal()
    row = db.get(ChannelInstance, instance_id)
    row.session_encrypted = "encrypted-login-secret"
    row.login_state = {"token": "sensitive-login-token"}
    db.commit()
    db.close()

    listed = client.get("/api/admin/channel-instances", headers=headers)
    assert listed.status_code == 200
    public = next(item for item in listed.json() if item["id"] == instance_id)
    assert public["status"] == "offline"
    assert "session_encrypted" not in public
    assert "login_state" not in public
    assert "sensitive-login-token" not in str(public)

    adapter = registry.get("wechat_clawbot")
    assert isinstance(adapter, ClawBotAdapter)


@pytest.mark.anyio
async def test_channel_instance_start_failure_is_recorded_without_leaking_details(monkeypatch):
    from wecom_ai_gateway.channels import registry
    from wecom_ai_gateway.db import SessionLocal
    from wecom_ai_gateway.models import ChannelInstance

    headers = {"X-Admin-Token": "test-admin-token"}
    created = client.post(
        "/api/admin/channel-instances",
        headers=headers,
        json={"channel": "wechat_clawbot", "instance_name": "故障桥接"},
    )
    instance_id = created.json()["id"]
    adapter = registry.get("wechat_clawbot")

    async def fail_start(_instance_id):
        raise RuntimeError("bridge failure access_token=never-leak")

    monkeypatch.setattr(adapter, "start_instance", fail_start)
    response = client.post(f"/api/admin/channel-instances/{instance_id}/start", headers=headers)
    assert response.status_code == 502
    assert "never-leak" not in response.text
    db = SessionLocal()
    assert db.get(ChannelInstance, instance_id).status == "error"
    db.close()


def test_internal_channel_message_reuses_generic_ingestion_and_is_idempotent(monkeypatch):
    from wecom_ai_gateway.config import settings
    from wecom_ai_gateway.db import SessionLocal
    from wecom_ai_gateway.models import ChannelInstance, OutboxTask

    monkeypatch.setattr(settings, "clawbot_bridge_token", "bridge-test-token")
    headers = {"X-Admin-Token": "test-admin-token"}
    created = client.post(
        "/api/admin/channel-instances",
        headers=headers,
        json={"channel": "wechat_clawbot", "instance_name": "在线桥接"},
    )
    instance_id = created.json()["id"]
    db = SessionLocal()
    db.get(ChannelInstance, instance_id).status = "online"
    db.commit()
    db.close()

    payload = {
        "sender_id": "wechat-user-1",
        "external_message_id": "bridge-inbound-1",
        "content": "来自桥接的消息",
        "raw": {"kind": "chat"},
    }
    first = client.post(
        f"/api/internal/channel-instances/{instance_id}/messages",
        headers={"Authorization": "Bearer bridge-test-token"},
        json=payload,
    )
    second = client.post(
        f"/api/internal/channel-instances/{instance_id}/messages",
        headers={"Authorization": "Bearer bridge-test-token"},
        json=payload,
    )
    assert first.status_code == 200 and first.json()["accepted"] is True
    assert second.status_code == 200 and second.json()["accepted"] is False

    db = SessionLocal()
    assert db.query(OutboxTask).filter_by(dedupe_key=f"message:{first.json()['message_id']}").count() == 1
    db.close()


def test_internal_channel_message_rejects_admin_token_without_bridge_token():
    response = client.post(
        "/api/internal/channel-instances/missing/messages",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"sender_id": "u", "external_message_id": "m"},
    )
    assert response.status_code in {401, 503}
