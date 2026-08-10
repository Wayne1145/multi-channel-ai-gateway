"""设置覆盖框架测试：用户/渠道级覆盖、优先级、管理 API、视图标记。"""

from fastapi.testclient import TestClient

from wecom_ai_gateway.main import app
from wecom_ai_gateway.models import PlatformConfig, SettingOverride, User
from wecom_ai_gateway.runtime_settings import (
    get_effective_value,
    list_overrides,
    remove_override,
    set_override,
    settings_view,
)

client = TestClient(app)
ADMIN_HEADERS = {"X-Admin-Token": "test-admin-token"}


def test_user_override_beats_platform_value(db):
    db.add(PlatformConfig(key="user_daily_token_quota", value=50000))
    db.commit()
    set_override(db, "user", "user-1", "user_daily_token_quota", 1000)

    assert get_effective_value(db, "user_daily_token_quota", user_id="user-1") == 1000
    assert get_effective_value(db, "user_daily_token_quota", user_id="user-2") == 50000


def test_channel_override_beats_platform_value(db):
    db.add(PlatformConfig(key="message_max_chars", value=10000))
    db.commit()
    set_override(db, "channel", "wechat_clawbot", "message_max_chars", 500)

    assert get_effective_value(db, "message_max_chars", channel="wechat_clawbot") == 500
    assert get_effective_value(db, "message_max_chars", channel="wecom_kf") == 10000


def test_user_override_wins_over_channel_and_platform(db):
    set_override(db, "channel", "wechat_clawbot", "max_context_messages", 30)
    set_override(db, "user", "user-9", "max_context_messages", 5)

    value = get_effective_value(
        db, "max_context_messages", user_id="user-9", channel="wechat_clawbot"
    )
    assert value == 5


def test_override_rejects_non_overridable_key(db):
    try:
        set_override(db, "user", "user-1", "database_url", "evil")
        raise AssertionError("database_url 不应允许覆盖")
    except ValueError:
        pass


def test_override_validation_enforces_min_max(db):
    try:
        set_override(db, "user", "user-1", "user_daily_token_quota", 1)
        raise AssertionError("低于 min 的值不应允许")
    except ValueError:
        pass


def test_remove_override_restores_platform_value(db):
    set_override(db, "user", "user-1", "user_daily_token_quota", 1000)
    remove_override(db, "user", "user-1", "user_daily_token_quota")
    assert get_effective_value(db, "user_daily_token_quota", user_id="user-1") is not None
    assert db.query(SettingOverride).filter_by(scope_type="user", scope_id="user-1").count() == 0


def test_list_overrides_groups_by_scope(db):
    set_override(db, "user", "user-1", "user_daily_token_quota", 1000)
    set_override(db, "channel", "wechat_clawbot", "message_max_chars", 500)
    rows = list_overrides(db)
    assert len(rows) == 2
    assert {r.scope_type for r in rows} == {"user", "channel"}


def test_settings_view_marks_overridable_keys(db):
    view = {item["key"]: item for item in settings_view(db)}
    assert view["user_daily_token_quota"]["overridable"] == ["user"]
    assert view["max_context_messages"]["overridable"] == ["channel", "user"]
    assert "overridable" not in view["platform_name"]


def test_admin_api_crud_and_tenant_guard(db):
    user = User(display_name="O", mode="self_service")
    db.add(user)
    db.commit()

    put = client.put(
        "/api/admin/settings/overrides",
        headers=ADMIN_HEADERS,
        json={"scope_type": "user", "scope_id": user.id, "key": "user_daily_token_quota", "value": 8888},
    )
    assert put.status_code == 200
    listing = client.get("/api/admin/settings/overrides", headers=ADMIN_HEADERS).json()
    assert any(o["scope_id"] == user.id for o in listing["overrides"])
    delete_id = next(o["id"] for o in listing["overrides"] if o["scope_id"] == user.id)
    deleted = client.delete(
        f"/api/admin/settings/overrides/{delete_id}", headers=ADMIN_HEADERS
    )
    assert deleted.status_code == 200
    assert client.get("/api/admin/settings/overrides", headers=ADMIN_HEADERS).json()["overrides"] == []


def test_admin_override_api_requires_admin():
    response = client.put(
        "/api/admin/settings/overrides",
        json={"scope_type": "user", "scope_id": "x", "key": "user_daily_token_quota", "value": 1},
    )
    assert response.status_code in (401, 403)
