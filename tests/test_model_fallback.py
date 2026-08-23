"""平台默认线路的 fallback 供应商切换测试。

用户诉求：SenseNova 报错/长时间无回复时自动切 DeepSeek，保证一个服务挂了
另一个能接上。实现位置在 _complete_ai 的平台单供应商路径（非模型组、非 BYOK）。

安全不变量：
- BYOK 用户自带密钥不参与回退（避免静默消耗平台额度、掩盖 BYOK 损坏）
- 鉴权类错误（401/403）不切换（配置问题必须暴露给管理员）
- fallback 未配置时保持原单供应商行为，抛原错误
"""
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from wecom_ai_gateway.config import settings
from wecom_ai_gateway.models import (
    Conversation,
    Message,
    MessageDirection,
    MessageStatus,
    UsageRecord,
    User,
    UserProvider,
    UserSettings,
)
from wecom_ai_gateway.providers import CompletionResult, RetryableProviderError
from wecom_ai_gateway.security import encrypt_secret
from wecom_ai_gateway.services import _complete_ai


def _setup_conversation(db, *, byok: bool = False, api_key: str = "platform-key"):
    user = User()
    db.add(user)
    db.flush()
    user_settings = UserSettings(user_id=user.id)
    if byok:
        provider = UserProvider(
            user_id=user.id,
            provider_key="openai-compatible",
            base_url="https://byok.example/v1",
            api_key_encrypted=encrypt_secret("byok-key"),
        )
        db.add(provider)
        db.flush()
        user_settings.provider_key = f"byok:{provider.id}"
    conversation = Conversation(user_id=user.id)
    row = Message(
        user_id=user.id,
        conversation_id=conversation.id,
        channel="wecom_kf",
        external_message_id="msg-fallback",
        direction="inbound",
        message_type="text",
        content="你好",
        status=MessageStatus.processing,
    )
    db.add_all([user_settings, conversation, row])
    db.commit()
    return row, conversation, user_settings


@pytest.mark.anyio
async def test_primary_timeout_fails_over_to_fallback(db, monkeypatch):
    """主线路超时后自动切到 fallback 供应商并返回内容。"""
    from wecom_ai_gateway import services as svc

    monkeypatch.setattr(settings, "fallback_model", "deepseek-chat")
    monkeypatch.setattr(settings, "fallback_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "fallback_api_key", "fallback-key")

    row, conversation, user_settings = _setup_conversation(db)
    db.close()

    primary = AsyncMock()
    primary.complete.side_effect = httpx.ReadTimeout("上游超时")
    fallback = AsyncMock()
    fallback.complete.return_value = CompletionResult(
        content="DeepSeek 回复", prompt_tokens=5, completion_tokens=3
    )
    calls = []

    def fake_provider(name, base_url, api_key, timeout):
        calls.append((base_url, api_key))
        return primary if "sensenova" in base_url or "primary" in base_url else fallback

    monkeypatch.setattr(svc, "provider_for", fake_provider)
    monkeypatch.setattr(
        svc,
        "resolve_provider",
        lambda db, us: ("openai-compatible", "https://primary.example/v1", "primary-key"),
    )

    answer = await svc._complete_ai(db, row, conversation, user_settings)

    assert answer == "DeepSeek 回复"
    assert len(calls) == 2
    assert calls[0][1] == "primary-key"
    assert calls[1][1] == "fallback-key"
    # 用量记录写实际命中供应商
    db = db
    usage = db.query(UsageRecord).all()
    assert usage
    assert "fallback" in usage[0].provider
    assert usage[0].model == "deepseek-chat"


@pytest.mark.anyio
async def test_authentication_error_does_not_fail_over(db, monkeypatch):
    """鉴权错误（401/403）不切换，直接暴露配置问题。"""
    from wecom_ai_gateway import services as svc

    monkeypatch.setattr(settings, "fallback_model", "deepseek-chat")
    monkeypatch.setattr(settings, "fallback_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "fallback_api_key", "fallback-key")

    row, conversation, user_settings = _setup_conversation(db)
    db.close()

    request = httpx.Request("POST", "https://primary.example/v1/chat/completions")
    response = httpx.Response(401, request=request)
    primary = AsyncMock()
    primary.complete.side_effect = httpx.HTTPStatusError(
        "unauthorized", request=request, response=response
    )
    factory = Mock(return_value=primary)
    monkeypatch.setattr(svc, "provider_for", factory)
    monkeypatch.setattr(
        svc,
        "resolve_provider",
        lambda db, us: ("openai-compatible", "https://primary.example/v1", "bad-key"),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await svc._complete_ai(db, row, conversation, user_settings)

    assert factory.call_count == 1


@pytest.mark.anyio
async def test_no_fallback_configured_keeps_original_error(db, monkeypatch):
    """fallback 未配置时保持原单供应商行为，抛原错误。"""
    from wecom_ai_gateway import services as svc

    monkeypatch.setattr(settings, "fallback_model", "")
    monkeypatch.setattr(settings, "fallback_base_url", "")
    monkeypatch.setattr(settings, "fallback_api_key", "")

    row, conversation, user_settings = _setup_conversation(db)
    db.close()

    primary = AsyncMock()
    primary.complete.side_effect = httpx.ReadTimeout("上游超时")
    factory = Mock(return_value=primary)
    monkeypatch.setattr(svc, "provider_for", factory)
    monkeypatch.setattr(
        svc,
        "resolve_provider",
        lambda db, us: ("openai-compatible", "https://primary.example/v1", "primary-key"),
    )

    with pytest.raises(httpx.ReadTimeout):
        await svc._complete_ai(db, row, conversation, user_settings)

    assert factory.call_count == 1


@pytest.mark.anyio
async def test_byok_never_fails_over_to_platform_fallback(db, monkeypatch):
    """BYOK 用户即使主线路失败也不得静默消耗平台 fallback 额度。"""
    from wecom_ai_gateway import services as svc

    monkeypatch.setattr(settings, "fallback_model", "deepseek-chat")
    monkeypatch.setattr(settings, "fallback_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "fallback_api_key", "fallback-key")

    row, conversation, user_settings = _setup_conversation(db, byok=True)
    db.close()

    primary = AsyncMock()
    primary.complete.side_effect = httpx.ReadTimeout("BYOK 上游超时")
    factory = Mock(return_value=primary)
    monkeypatch.setattr(svc, "provider_for", factory)

    with pytest.raises(httpx.ReadTimeout):
        await svc._complete_ai(db, row, conversation, user_settings)

    assert factory.call_count == 1


@pytest.mark.anyio
async def test_retryable_empty_reply_fails_over(db, monkeypatch):
    """供应商返回空内容（RetryableProviderError）也触发 fallback。"""
    from wecom_ai_gateway import services as svc

    monkeypatch.setattr(settings, "fallback_model", "deepseek-chat")
    monkeypatch.setattr(settings, "fallback_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "fallback_api_key", "fallback-key")

    row, conversation, user_settings = _setup_conversation(db)
    db.close()

    primary = AsyncMock()
    primary.complete.side_effect = RetryableProviderError("模型尚未生成可发送的最终内容")
    fallback = AsyncMock()
    fallback.complete.return_value = CompletionResult(
        content="备用回复", prompt_tokens=2, completion_tokens=2
    )
    calls = []

    def fake_provider(name, base_url, api_key, timeout):
        calls.append(base_url)
        return primary if "primary" in base_url else fallback

    monkeypatch.setattr(svc, "provider_for", fake_provider)
    monkeypatch.setattr(
        svc,
        "resolve_provider",
        lambda db, us: ("openai-compatible", "https://primary.example/v1", "primary-key"),
    )

    answer = await svc._complete_ai(db, row, conversation, user_settings)
    assert answer == "备用回复"
    assert len(calls) == 2