"""运行时设置注册表测试：默认回退、DB 覆盖、校验、敏感项与模式特例。"""

from wecom_ai_gateway.config import settings
from wecom_ai_gateway.models import PlatformConfig
from wecom_ai_gateway.runtime_settings import (
    SPEC_BY_KEY,
    get_runtime_value,
    settings_view,
    update_settings,
)


def test_default_falls_back_to_env_value(db):
    """无 DB 行时返回 .env 默认值（保留现有 .env 语义）。"""
    assert get_runtime_value(db, "user_daily_token_quota") == settings.user_daily_token_quota
    assert get_runtime_value(db, "task_max_attempts") == settings.task_max_attempts


def test_db_value_overrides_env_default(db, monkeypatch):
    monkeypatch.setattr(settings, "user_daily_token_quota", 100000)
    db.add(PlatformConfig(key="user_daily_token_quota", value=50000))
    db.commit()
    assert get_runtime_value(db, "user_daily_token_quota") == 50000


def test_update_validates_min_max_and_rejects_atomic(db):
    db.add(PlatformConfig(key="task_max_attempts", value=5))
    db.commit()

    errors = update_settings(db, {"task_max_attempts": 9999})
    assert "task_max_attempts" in errors
    # 非法值不落库
    assert get_runtime_value(db, "task_max_attempts") == 5


def test_update_rejects_unknown_and_secret_keys(db):
    errors = update_settings(db, {"no_such_key": 1, "openai_compatible_api_key": "sk-hack"})
    assert "no_such_key" in errors
    assert "openai_compatible_api_key" in errors
    assert db.get(PlatformConfig, "no_such_key") is None


def test_update_rejects_invalid_enum_option(db):
    errors = update_settings(db, {"platform_mode": "chaotic_mode"})
    assert "platform_mode" in errors


def test_update_valid_values_persist(db):
    errors = update_settings(
        db,
        {
            "task_max_attempts": 7,
            "media_max_size_bytes": 5 * 1024 * 1024,
            "allow_public_registration": True,
            "announcement": "系统维护公告",
        },
    )
    assert errors == {}
    assert get_runtime_value(db, "task_max_attempts") == 7
    assert get_runtime_value(db, "media_max_size_bytes") == 5 * 1024 * 1024
    assert get_runtime_value(db, "allow_public_registration") is True
    assert get_runtime_value(db, "announcement") == "系统维护公告"


def test_platform_mode_maps_to_existing_platform_config_mode_key(db):
    """兼容既有 policy.resolve_user_mode 读取的 platform_config['mode']。"""
    update_settings(db, {"platform_mode": "managed"})
    row = db.get(PlatformConfig, "mode")
    assert row is not None
    assert row.value == {"mode": "managed"}
    assert get_runtime_value(db, "platform_mode") == "managed"


def test_settings_view_exposes_metadata_and_redacts_secrets(db, monkeypatch):
    monkeypatch.setattr(settings, "openai_compatible_api_key", "sk-real-key")
    view = settings_view(db)
    by_key = {item["key"]: item for item in view}
    assert "user_daily_token_quota" in by_key
    quota = by_key["user_daily_token_quota"]
    assert quota["type"] == "int"
    assert quota["min"] is not None and quota["max"] is not None
    # 敏感项只暴露 configured 状态，绝不返回内容
    secret = by_key["openai_compatible_api_key"]
    assert secret["secret"] is True
    assert secret["value"] == {"configured": True}
    assert "sk-real-key" not in str(view)


def test_secret_without_env_shows_unconfigured(db):
    view = {item["key"]: item for item in settings_view(db)}
    assert view["openai_compatible_api_key"]["value"] == {"configured": False}


def test_specs_are_well_formed():
    for spec in SPEC_BY_KEY.values():
        assert spec.key
        assert spec.group in {"general", "model", "account", "quota", "content", "media", "task", "channel", "retention", "alert"}
        assert spec.label
        assert spec.type in {"int", "float", "bool", "str", "select", "secret"}
        if spec.type == "select":
            assert spec.options
        if spec.type in {"int", "float"}:
            assert spec.min is not None and spec.max is not None
