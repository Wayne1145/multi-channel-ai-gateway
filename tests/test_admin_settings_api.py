"""管理设置 API 与用户配额展示测试。"""

from fastapi.testclient import TestClient

from wecom_ai_gateway.db import SessionLocal
from wecom_ai_gateway.main import app
from wecom_ai_gateway.models import Account, AuditLog, PlatformConfig, User, UserSettings
from wecom_ai_gateway.runtime_settings import update_settings
from wecom_ai_gateway.security import hash_password

client = TestClient(app)

ADMIN_HEADERS = {"X-Admin-Token": "test-admin-token"}


def _user_with_account(username="erin") -> str:
    db = SessionLocal()
    user = User(display_name="Erin", mode="self_service")
    db.add(user)
    db.flush()
    db.add(UserSettings(user_id=user.id))
    db.add(Account(user_id=user.id, username=username, password_hash=hash_password("strong-pass-123"), role="user"))
    db.commit()
    user_id = user.id
    db.close()
    return user_id


def test_admin_settings_get_returns_groups_and_secret_redaction():
    response = client.get("/api/admin/settings", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    settings = response.json()["settings"]
    groups = {item["group"] for item in settings}
    assert {"general", "model", "account", "quota", "content", "media", "task", "channel", "retention"} <= groups
    secret = next(item for item in settings if item["key"] == "openai_compatible_api_key")
    assert secret["secret"] is True
    assert secret["value"] == {"configured": False}
    assert "sk-" not in str(response.json())


def test_admin_tool_catalog_is_read_only_and_reflects_runtime_settings(db):
    update_settings(
        db,
        {
            "tools_enabled": True,
            "tools_allowed": "get_weather",
            "tool_max_calls": 2,
            "tool_timeout_seconds": 7,
        },
    )
    assert client.get("/api/admin/tools").status_code == 401

    response = client.get("/api/admin/tools", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["allowed"] == ["get_weather"]
    assert body["max_calls"] == 2
    assert body["timeout_seconds"] == 7
    assert {item["name"] for item in body["catalog"]} == {
        "get_current_time",
        "get_weather",
    }
    assert all(item["read_only"] is True for item in body["catalog"])


def test_admin_settings_put_validates_and_rejects_bad_values():
    bad = client.put(
        "/api/admin/settings",
        headers=ADMIN_HEADERS,
        json={"values": {"task_max_attempts": 9999}},
    )
    assert bad.status_code == 400
    assert "task_max_attempts" in bad.json()["detail"]["errors"]

    ok = client.put(
        "/api/admin/settings",
        headers=ADMIN_HEADERS,
        json={"values": {"task_max_attempts": 7, "announcement": "升级公告"}},
    )
    assert ok.status_code == 200
    db = SessionLocal()
    assert db.get(PlatformConfig, "task_max_attempts").value == 7
    audit = db.query(AuditLog).filter_by(action="settings.update").count()
    assert audit >= 1
    db.close()


def test_settings_put_requires_admin():
    response = client.put("/api/admin/settings", json={"values": {"task_max_attempts": 7}})
    assert response.status_code == 401 or response.status_code == 403


def test_me_summary_exposes_quota_fields(db):
    _user_with_account()
    token = client.post(
        "/api/auth/login", json={"username": "erin", "password": "strong-pass-123"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get("/api/me/summary", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["quota"]["enabled"] is True
    assert body["quota"]["quota"] > 0
    assert body["quota"]["used"] >= 0
    assert body["quota"]["remaining"] >= 0
    assert 50 <= body["quota"]["alert_threshold"] <= 100


def test_me_summary_quota_reflects_runtime_value(db):
    _user_with_account("frank")
    update_settings(db, {"user_daily_token_quota": 12345})
    token = client.post(
        "/api/auth/login", json={"username": "frank", "password": "strong-pass-123"}
    ).json()["token"]

    body = client.get("/api/me/summary", headers={"Authorization": f"Bearer {token}"}).json()

    assert body["quota"]["quota"] == 12345


def test_audit_logs_endpoint_requires_admin_and_returns_rows(db):
    unauthorized = client.get("/api/admin/audit-logs")
    assert unauthorized.status_code in (401, 403)

    db.add(AuditLog(action="settings.update", detail={"count": 1}))
    db.commit()
    response = client.get("/api/admin/audit-logs", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    rows = response.json()
    assert any(row["action"] == "settings.update" for row in rows)


def test_runtime_value_cache_hits_within_session(db):
    from wecom_ai_gateway.runtime_settings import get_runtime_value

    first = get_runtime_value(db, "task_max_attempts")
    assert db.info["_runtime_settings_cache"]["task_max_attempts"] == first
    second = get_runtime_value(db, "task_max_attempts")
    assert second == first


def test_update_settings_invalidates_runtime_cache(db):
    from wecom_ai_gateway.runtime_settings import get_runtime_value

    get_runtime_value(db, "task_max_attempts")
    assert "_runtime_settings_cache" in db.info
    update_settings(db, {"task_max_attempts": 7})
    assert "_runtime_settings_cache" not in db.info
