"""平台模型组与故障切换测试。"""

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from wecom_ai_gateway.model_routing import complete_with_routing
from wecom_ai_gateway.models import (
    Conversation,
    Message,
    MessageStatus,
    ModelGroup,
    ModelRoute,
    PlatformProvider,
    UsageRecord,
    User,
    UserProvider,
    UserSettings,
)
from wecom_ai_gateway.providers import CompletionResult
from wecom_ai_gateway.security import encrypt_secret
from wecom_ai_gateway.services import _complete_ai


def _provider(db, name: str, url: str, key: str, *, enabled: bool = True) -> PlatformProvider:
    row = PlatformProvider(
        name=name,
        provider_key="openai-compatible",
        base_url=url,
        api_key_encrypted=encrypt_secret(key),
        enabled=enabled,
    )
    db.add(row)
    db.flush()
    return row


def test_database_rejects_multiple_default_groups(db):
    from sqlalchemy.exc import IntegrityError

    db.add_all([
        ModelGroup(name="默认组一", is_default=True, enabled=True),
        ModelGroup(name="默认组二", is_default=True, enabled=True),
    ])
    with pytest.raises(IntegrityError):
        db.commit()


def _group(db, *routes: tuple[PlatformProvider, str, int]) -> ModelGroup:
    group = ModelGroup(name="默认模型组", is_default=True, enabled=True)
    db.add(group)
    db.flush()
    for provider, model, priority in routes:
        db.add(
            ModelRoute(
                group_id=group.id,
                provider_id=provider.id,
                model=model,
                priority=priority,
                enabled=True,
            )
        )
    db.flush()
    return group


@pytest.mark.anyio
async def test_timeout_fails_over_in_priority_order(db, monkeypatch):
    primary = _provider(db, "主线路", "https://primary.example/v1", "key-primary")
    backup = _provider(db, "备用线路", "https://backup.example/v1", "key-backup")
    group = _group(db, (backup, "backup-model", 20), (primary, "primary-model", 10))
    db.commit()

    first = AsyncMock()
    first.complete.side_effect = httpx.ReadTimeout("上游超时")
    second = AsyncMock()
    second.complete.return_value = CompletionResult(
        content="备用线路回复", prompt_tokens=12, completion_tokens=8
    )
    created = []

    def fake_provider(name, base_url, api_key, timeout):
        created.append((name, base_url, api_key, timeout))
        return first if len(created) == 1 else second

    monkeypatch.setattr("wecom_ai_gateway.model_routing.provider_for", fake_provider)
    result = await complete_with_routing(
        db,
        UserSettings(user_id="user-1"),
        [{"role": "user", "content": "你好"}],
        temperature=0.7,
        max_tokens=1024,
        timeout=30,
    )

    assert [item[1] for item in created] == [
        "https://primary.example/v1",
        "https://backup.example/v1",
    ]
    assert result.content == "备用线路回复"
    assert result.provider_name == "备用线路"
    assert result.provider_key == "openai-compatible"
    assert result.model == "backup-model"
    assert result.group_id == group.id
    assert result.route_id is not None


@pytest.mark.anyio
async def test_authentication_error_does_not_hide_configuration_problem(db, monkeypatch):
    primary = _provider(db, "主线路", "https://primary.example/v1", "bad-key")
    backup = _provider(db, "备用线路", "https://backup.example/v1", "good-key")
    _group(db, (primary, "primary-model", 10), (backup, "backup-model", 20))
    db.commit()

    request = httpx.Request("POST", "https://primary.example/v1/chat/completions")
    response = httpx.Response(401, request=request)
    provider = AsyncMock()
    provider.complete.side_effect = httpx.HTTPStatusError(
        "unauthorized", request=request, response=response
    )
    factory = Mock(return_value=provider)
    monkeypatch.setattr("wecom_ai_gateway.model_routing.provider_for", factory)

    with pytest.raises(httpx.HTTPStatusError):
        await complete_with_routing(
            db,
            UserSettings(user_id="user-1"),
            [{"role": "user", "content": "你好"}],
            temperature=0.7,
            max_tokens=1024,
            timeout=30,
        )

    assert factory.call_count == 1


@pytest.mark.anyio
async def test_runtime_configuration_error_does_not_fail_over(db, monkeypatch):
    primary = _provider(db, "主线路", "https://primary.example/v1", "key-primary")
    backup = _provider(db, "备用线路", "https://backup.example/v1", "key-backup")
    _group(db, (primary, "primary-model", 10), (backup, "backup-model", 20))
    db.commit()

    provider = AsyncMock()
    provider.complete.side_effect = RuntimeError("供应商配置错误")
    factory = Mock(return_value=provider)
    monkeypatch.setattr("wecom_ai_gateway.model_routing.provider_for", factory)

    with pytest.raises(RuntimeError, match="配置错误"):
        await complete_with_routing(
            db,
            UserSettings(user_id="user-1"),
            [{"role": "user", "content": "你好"}],
            temperature=0.7,
            max_tokens=1024,
            timeout=30,
        )

    assert factory.call_count == 1


@pytest.mark.anyio
async def test_disabled_routes_and_providers_are_skipped(db, monkeypatch):
    disabled_provider = _provider(
        db, "停用供应商", "https://disabled.example/v1", "disabled", enabled=False
    )
    active_provider = _provider(db, "可用供应商", "https://active.example/v1", "active")
    group = _group(
        db,
        (disabled_provider, "disabled-model", 1),
        (active_provider, "active-model", 20),
    )
    disabled_route = ModelRoute(
        group_id=group.id,
        provider_id=active_provider.id,
        model="route-disabled-model",
        priority=10,
        enabled=False,
    )
    db.add(disabled_route)
    db.commit()

    provider = AsyncMock()
    provider.complete.return_value = CompletionResult(content="ok")
    factory = Mock(return_value=provider)
    monkeypatch.setattr("wecom_ai_gateway.model_routing.provider_for", factory)

    result = await complete_with_routing(
        db,
        UserSettings(user_id="user-1"),
        [{"role": "user", "content": "你好"}],
        temperature=0.7,
        max_tokens=1024,
        timeout=30,
    )

    assert factory.call_count == 1
    assert factory.call_args.args[1] == "https://active.example/v1"
    assert result.model == "active-model"


def _message_context(db, *, provider_key: str | None = None, model: str | None = None):
    user = User()
    db.add(user)
    db.flush()
    user_settings = UserSettings(user_id=user.id, provider_key=provider_key, model=model)
    conversation = Conversation(user_id=user.id)
    db.add_all([user_settings, conversation])
    db.flush()
    row = Message(
        user_id=user.id,
        conversation_id=conversation.id,
        channel="wecom_kf",
        external_message_id=f"routing-{user.id}",
        direction="inbound",
        message_type="text",
        content="你好",
        status=MessageStatus.processing,
    )
    db.add(row)
    db.flush()
    return user_settings, conversation, row


@pytest.mark.anyio
async def test_complete_ai_uses_default_group_and_records_actual_route(db, monkeypatch):
    primary = _provider(db, "主线路", "https://primary.example/v1", "primary")
    backup = _provider(db, "备用线路", "https://backup.example/v1", "backup")
    _group(db, (primary, "primary-model", 10), (backup, "backup-model", 20))
    user_settings, conversation, row = _message_context(db)
    db.commit()

    first = AsyncMock()
    first.complete.side_effect = httpx.ReadTimeout("timeout")
    second = AsyncMock()
    second.complete.return_value = CompletionResult(
        content="从备用线路返回", prompt_tokens=20, completion_tokens=10
    )
    factory = Mock(side_effect=[first, second])
    monkeypatch.setattr("wecom_ai_gateway.model_routing.provider_for", factory)

    answer = await _complete_ai(db, row, conversation, user_settings)

    assert answer == "从备用线路返回"
    usage = db.query(UsageRecord).one()
    assert usage.provider == "备用线路"
    assert usage.model == "backup-model"
    assert usage.prompt_tokens == 20
    assert usage.completion_tokens == 10


@pytest.mark.anyio
async def test_byok_bypasses_platform_model_group(db, monkeypatch):
    platform = _provider(db, "平台线路", "https://platform.example/v1", "platform")
    _group(db, (platform, "platform-model", 10))
    user_settings, conversation, row = _message_context(db, model="byok-model")
    byok = UserProvider(
        user_id=row.user_id,
        provider_key="openai-compatible",
        base_url="https://byok.example/v1",
        api_key_encrypted=encrypt_secret("byok-key"),
        models=["byok-model"],
    )
    db.add(byok)
    db.flush()
    user_settings.provider_key = f"byok:{byok.id}"
    db.commit()

    provider = AsyncMock()
    provider.complete.return_value = CompletionResult(content="BYOK 回复")
    direct_factory = Mock(return_value=provider)
    platform_factory = Mock(side_effect=AssertionError("BYOK 不应进入平台模型组"))
    monkeypatch.setattr("wecom_ai_gateway.services.provider_for", direct_factory)
    monkeypatch.setattr("wecom_ai_gateway.model_routing.provider_for", platform_factory)

    answer = await _complete_ai(db, row, conversation, user_settings)

    assert answer == "BYOK 回复"
    assert direct_factory.call_args.args[1] == "https://byok.example/v1"
    assert direct_factory.call_args.args[2] == "byok-key"
    assert db.query(UsageRecord).one().model == "byok-model"


@pytest.mark.anyio
async def test_stale_byok_never_falls_back_to_platform_secret(db, monkeypatch):
    user_settings, conversation, row = _message_context(
        db, provider_key="byok:missing", model="private-model"
    )
    platform_factory = Mock(side_effect=AssertionError("不得混用平台密钥"))
    monkeypatch.setattr("wecom_ai_gateway.services.provider_for", platform_factory)
    monkeypatch.setattr(
        "wecom_ai_gateway.services.settings.openai_compatible_api_key", "platform-secret"
    )

    with pytest.raises(RuntimeError, match="BYOK"):
        await _complete_ai(db, row, conversation, user_settings)

    platform_factory.assert_not_called()


@pytest.mark.anyio
async def test_env_provider_remains_fallback_when_no_model_group(db, monkeypatch):
    user_settings, conversation, row = _message_context(db)
    provider = AsyncMock()
    provider.complete.return_value = CompletionResult(content="兼容回退")
    factory = Mock(return_value=provider)
    monkeypatch.setattr(
        "wecom_ai_gateway.services.resolve_provider",
        Mock(return_value=("openai-compatible", "https://legacy.example/v1", "legacy-key")),
    )
    monkeypatch.setattr("wecom_ai_gateway.services.provider_for", factory)

    answer = await _complete_ai(db, row, conversation, user_settings)

    assert answer == "兼容回退"
    assert factory.call_args.args[1] == "https://legacy.example/v1"


@pytest.mark.anyio
async def test_user_assigned_group_overrides_platform_default(db, monkeypatch):
    default_provider = _provider(db, "默认线路", "https://default.example/v1", "default")
    assigned_provider = _provider(db, "专属线路", "https://assigned.example/v1", "assigned")
    _group(db, (default_provider, "default-model", 10))
    assigned_group = ModelGroup(name="专属模型组", is_default=False, enabled=True)
    db.add(assigned_group)
    db.flush()
    db.add(
        ModelRoute(
            group_id=assigned_group.id,
            provider_id=assigned_provider.id,
            model="assigned-model",
            priority=10,
            enabled=True,
        )
    )
    user_settings, conversation, row = _message_context(db)
    user_settings.model_group_id = assigned_group.id
    db.commit()

    provider = AsyncMock()
    provider.complete.return_value = CompletionResult(content="专属组回复")
    factory = Mock(return_value=provider)
    monkeypatch.setattr("wecom_ai_gateway.model_routing.provider_for", factory)

    answer = await _complete_ai(db, row, conversation, user_settings)

    assert answer == "专属组回复"
    assert factory.call_args.args[1] == "https://assigned.example/v1"
    assert db.query(UsageRecord).one().model == "assigned-model"
