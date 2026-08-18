"""用户自助中心测试：角色卡/预设/记忆/BYOK/账号安全/用量。"""

from fastapi.testclient import TestClient

from wecom_ai_gateway.db import SessionLocal
from wecom_ai_gateway.main import app
from wecom_ai_gateway.models import (
    Account,
    User,
    UserSettings,
)
from wecom_ai_gateway.runtime_settings import update_settings
from wecom_ai_gateway.security import hash_password

client = TestClient(app)


def _login(username: str = "selfuser", password: str = "strong-pass-123") -> tuple[str, str]:
    db = SessionLocal()
    user = User(display_name="Self", mode="self_service")
    db.add(user)
    db.flush()
    db.add(UserSettings(user_id=user.id))
    db.add(
        Account(user_id=user.id, username=username, password_hash=hash_password(password), role="user")
    )
    db.commit()
    user_id = user.id
    db.close()
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["token"]
    return user_id, token


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_card_lifecycle_and_ownership(db):
    _, token = _login()
    _, other_token = _login("other")

    created = client.post(
        "/api/me/cards", headers=_headers(token), json={"name": "学者", "content": "你是一位学者"}
    ).json()
    assert created["name"] == "学者"

    detail = client.get(f"/api/me/cards/{created['id']}", headers=_headers(token)).json()
    assert detail["content"] == "你是一位学者"

    activated = client.post(
        f"/api/me/cards/{created['id']}/activate", headers=_headers(token)
    )
    assert activated.status_code == 200

    updated = client.put(
        f"/api/me/cards/{created['id']}",
        headers=_headers(token),
        json={"content": "你是一位严谨的学者"},
    )
    assert updated.status_code == 200

    # 越权：他人不可读/删
    assert client.get(f"/api/me/cards/{created['id']}", headers=_headers(other_token)).status_code == 404
    assert client.delete(f"/api/me/cards/{created['id']}", headers=_headers(other_token)).status_code == 404

    deleted = client.delete(f"/api/me/cards/{created['id']}", headers=_headers(token))
    assert deleted.status_code == 200


def test_card_count_limit(db):
    _, token = _login("limituser")
    update_settings(db, {"max_cards_per_user": 1})
    assert client.post("/api/me/cards", headers=_headers(token), json={"name": "一"}).status_code == 200
    second = client.post("/api/me/cards", headers=_headers(token), json={"name": "二"})
    assert second.status_code == 400
    assert "上限" in second.json()["detail"]


def test_preset_save_apply_delete(db):
    _, token = _login("presetuser")
    saved = client.post("/api/me/presets", headers=_headers(token), json={"name": "工作"})
    assert saved.status_code == 200
    rows = client.get("/api/me/presets", headers=_headers(token)).json()
    assert len(rows) == 1 and rows[0]["name"] == "工作"
    applied = client.post(f"/api/me/presets/{rows[0]['id']}/apply", headers=_headers(token))
    assert applied.status_code == 200
    deleted = client.delete(f"/api/me/presets/{rows[0]['id']}", headers=_headers(token))
    assert deleted.status_code == 200


def test_memory_add_list_delete_clear(db):
    _, token = _login("memuser")
    update_settings(db, {"memory_max_chars": 100, "max_memories_per_user": 2})
    add = client.post("/api/me/memories", headers=_headers(token), json={"content": "记" * 200})
    assert add.status_code == 200
    rows = client.get("/api/me/memories", headers=_headers(token)).json()
    assert len(rows) == 1
    assert len(rows[0]["content"]) == 100
    third = client.post("/api/me/memories", headers=_headers(token), json={"content": "二"})
    second = client.post("/api/me/memories", headers=_headers(token), json={"content": "三"})
    assert third.status_code == 200
    assert second.status_code == 400  # 数量上限
    cleared = client.post("/api/me/memories/clear", headers=_headers(token))
    assert cleared.status_code == 200
    assert client.get("/api/me/memories", headers=_headers(token)).json() == []


def test_provider_add_rejects_http_and_hides_key(db):
    _, token = _login("byokuser")
    bad = client.post(
        "/api/me/providers",
        headers=_headers(token),
        json={"provider_key": "openai-compatible", "base_url": "http://insecure", "api_key": "sk-x"},
    )
    assert bad.status_code == 400
    ok = client.post(
        "/api/me/providers",
        headers=_headers(token),
        json={
            "provider_key": "openai-compatible",
            "base_url": "https://byok.example/v1",
            "api_key": "sk-secret-123",
            "models": ["m1"],
        },
    )
    assert ok.status_code == 200
    rows = client.get("/api/me/providers", headers=_headers(token)).json()
    assert len(rows) == 1
    assert "sk-secret-123" not in str(rows)
    assert "api_key" not in rows[0]


def test_provider_count_limit(db):
    _, token = _login("byoklimit")
    update_settings(db, {"max_providers_per_user": 1})
    client.post(
        "/api/me/providers",
        headers=_headers(token),
        json={"provider_key": "p", "base_url": "https://a.example/v1", "api_key": "k1"},
    )
    second = client.post(
        "/api/me/providers",
        headers=_headers(token),
        json={"provider_key": "p", "base_url": "https://b.example/v1", "api_key": "k2"},
    )
    assert second.status_code == 400


def test_deleting_selected_byok_clears_user_provider_choice(db):
    user_id, token = _login("byokdelete")
    created = client.post(
        "/api/me/providers",
        headers=_headers(token),
        json={
            "provider_key": "openai-compatible",
            "base_url": "https://delete.example/v1",
            "api_key": "private-key",
        },
    ).json()
    db = SessionLocal()
    settings_row = db.get(UserSettings, user_id)
    settings_row.provider_key = f"byok:{created['id']}"
    db.commit()
    db.close()

    assert client.delete(
        f"/api/me/providers/{created['id']}", headers=_headers(token)
    ).status_code == 200
    db = SessionLocal()
    assert db.get(UserSettings, user_id).provider_key is None
    db.close()


def test_password_change_revokes_other_sessions(db):
    _, token = _login("passuser")
    db = SessionLocal()
    extra = client.post("/api/auth/login", json={"username": "passuser", "password": "strong-pass-123"}).json()["token"]
    db.close()

    changed = client.post(
        "/api/me/password",
        headers=_headers(token),
        json={"old_password": "strong-pass-123", "new_password": "new-pass-123456"},
    )
    assert changed.status_code == 200

    # 旧密码失效，新密码可登录
    assert client.post("/api/auth/login", json={"username": "passuser", "password": "strong-pass-123"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "passuser", "password": "new-pass-123456"}).status_code == 200
    # 修改前的其他会话已被撤销
    assert client.get("/api/auth/me", headers=_headers(extra)).status_code == 401


def test_sessions_list_and_revoke_all():
    _, token = _login("sessuser")
    second = client.post("/api/auth/login", json={"username": "sessuser", "password": "strong-pass-123"}).json()["token"]
    rows = client.get("/api/me/sessions", headers=_headers(token)).json()
    assert len(rows) >= 2
    revoked = client.post("/api/me/sessions/revoke-all", headers=_headers(token))
    assert revoked.status_code == 200
    assert client.get("/api/auth/me", headers=_headers(second)).status_code == 401
    assert client.get("/api/auth/me", headers=_headers(token)).status_code == 200


def test_usage_daily_aggregate(db):
    from datetime import UTC, datetime

    from wecom_ai_gateway.models import UsageRecord

    user_id, token = _login("usageuser")
    db = SessionLocal()
    db.add(
        UsageRecord(
            user_id=user_id,
            provider="p",
            model="m",
            prompt_tokens=100,
            completion_tokens=50,
            created_at=datetime.now(UTC),
        )
    )
    db.commit()
    db.close()

    rows = client.get("/api/me/usage?days=7", headers=_headers(token)).json()
    assert len(rows) >= 1
    assert rows[-1]["tokens"] == 150
