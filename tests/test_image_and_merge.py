"""图片多模态路由 + 连发消息合并回复 的测试。

需求：
1. 用户发图片 → 直接绕过 SenseNova，用 DeepSeek 多模态模型回答
2. 用户连发多条消息 → 合并成一次 AI 调用一起回复
3. DeepSeek 使用多模态模型 deepseek-v4-flash-vision-exp（不用 flash）
"""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from wecom_ai_gateway.config import settings
from wecom_ai_gateway.db import SessionLocal
from wecom_ai_gateway.models import (
    ChannelIdentity,
    Conversation,
    MediaAsset,
    Message,
    MessageStatus,
    User,
    UserSettings,
)
from wecom_ai_gateway.security import encrypt_secret
from wecom_ai_gateway.services import ingest


def _setup_user(db, username: str = "wmImageUser") -> tuple[str, str]:
    user = User(display_name=username)
    db.add(user)
    db.flush()
    identity = ChannelIdentity(
        user_id=user.id,
        channel="wecom_kf",
        account_id="wkImageAccount",
        external_id_hash=f"h{username}",
        external_id_encrypted=encrypt_secret(f"ext-{username}"),
    )
    db.add(identity)
    db.flush()
    return user.id, "wkImageAccount"


def test_image_message_is_queued_for_reply(db):
    """图片消息应进入回复队列（此前只回复文本，图片被静默忽略）。"""
    _user_id, _account_id = _setup_user(db)
    with patch("wecom_ai_gateway.services.encrypt_secret", side_effect=lambda v: f"enc:{v}"):
        ingest(
            db,
            {
                "msgid": "img-msg-1",
                "open_kfid": "wkImageAccount",
                "external_userid": "wmImageUser",
                "msgtype": "image",
                "origin": 3,
                "media_id": "media-abc123",
            },
        )
        db.commit()
    row = db.query(Message).filter_by(external_message_id="img-msg-1").one()
    assert row.status == MessageStatus.queued
    assert row.message_type == "image"
    media = db.query(MediaAsset).filter_by(message_id=row.id).one()
    assert media.media_type == "image"
    assert media.storage_key == "media-abc123"


def test_wecom_image_nested_media_id_is_extracted(db):
    """企微客服图片消息的 media_id 嵌套在 msgtype 字段里，必须正确提取。

    回归：旧实现用 item.get('media_id') 顶层取，storage_key 恒为 None，
    导致图片无法下载、用户收到'看不了图片'。企微格式为
    {"msgtype":"image","image":{"media_id":"...","format":"png"}}。
    """
    _user_id, _account_id = _setup_user(db)
    with patch("wecom_ai_gateway.services.encrypt_secret", side_effect=lambda v: f"enc:{v}"):
        ingest(
            db,
            {
                "msgid": "img-nested-1",
                "open_kfid": "wkImageAccount",
                "external_userid": "wmImageUser",
                "msgtype": "image",
                "origin": 3,
                "image": {"media_id": "NESTED_MEDIA_123", "format": "png"},
            },
        )
        db.commit()
    row = db.query(Message).filter_by(external_message_id="img-nested-1").one()
    assert row.status == MessageStatus.queued
    media = db.query(MediaAsset).filter_by(message_id=row.id).one()
    assert media.storage_key == "NESTED_MEDIA_123", "必须从嵌套 image 字段取 media_id"
    assert media.mime == "png"
    # 图片消息必须有占位 content，后续文字消息上下文才能看到“用户发过图”
    assert row.content == "[图片]"


def test_image_message_content_placeholder_keeps_context(db):
    """图片消息入库 content 用 [图片] 占位，避免后续文字消息看到空白上下文。

    回归：图片消息 content=None 时，用户随后追问的文字消息在 history 里看到
    一条空白 user 消息，模型误以为又是一张看不了的图，编造“看不了图片”回复。
    """
    _user_id, _account_id = _setup_user(db)
    with patch("wecom_ai_gateway.services.encrypt_secret", side_effect=lambda v: f"enc:{v}"):
        ingest(
            db,
            {
                "msgid": "img-placeholder-1",
                "open_kfid": "wkImageAccount",
                "external_userid": "wmImageUser",
                "msgtype": "image",
                "origin": 3,
                "image": {"media_id": "PH_MEDIA_1", "format": "png"},
            },
        )
        db.commit()
    row = db.query(Message).filter_by(external_message_id="img-placeholder-1").one()
    assert row.content == "[图片]"


def test_voice_message_still_ignored(db):
    """语音/文件消息仍不进入 AI 回复。"""
    _user_id, _account_id = _setup_user(db)
    with patch("wecom_ai_gateway.services.encrypt_secret", side_effect=lambda v: f"enc:{v}"):
        ingest(
            db,
            {
                "msgid": "voice-msg-1",
                "open_kfid": "wkImageAccount",
                "external_userid": "wmImageUser",
                "msgtype": "voice",
                "origin": 3,
                "media_id": "media-voice",
            },
        )
        db.commit()
    row = db.query(Message).filter_by(external_message_id="voice-msg-1").one()
    assert row.status == MessageStatus.ignored


@pytest.mark.anyio
async def test_image_message_routes_to_vision_model_bypassing_sensenova(db, monkeypatch):
    """图片消息必须绕过 SenseNova，直接走 DeepSeek 多模态模型。"""
    from wecom_ai_gateway import services as svc

    monkeypatch.setattr(settings, "fallback_model", "deepseek-v4-flash-vision-exp")
    monkeypatch.setattr(settings, "fallback_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "fallback_api_key", "fallback-key")

    user_id, account_id = _setup_user(db)
    conversation = Conversation(user_id=user_id)
    db.add(conversation)
    db.flush()
    row = Message(
        user_id=user_id,
        conversation_id=conversation.id,
        channel="wecom_kf",
        channel_instance_id=account_id,
        external_message_id="img-route-1",
        direction="inbound",
        message_type="image",
        content=None,
        status=MessageStatus.queued,
        metadata_json={"open_kfid": account_id, "media_types": ["image"], "media_count": 1},
    )
    db.add(row)
    db.flush()
    db.add(
        MediaAsset(
            message_id=row.id,
            channel="wecom_kf",
            media_type="image",
            mime="image/jpeg",
            storage_key="media-route-1",
            status="stored",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    db.commit()
    row_id = row.id
    db.close()

    # 记录 provider_for 收到的 base_url 与 model，验证：
    # 1) 只调用了一次（没有先打 SenseNova 再切）
    # 2) 走的是 DeepSeek 多模态
    calls = []

    async def fake_provider_complete(self, messages, model, temperature, max_tokens, **kw):
        calls.append((self._base_url, model))
        from wecom_ai_gateway.providers import CompletionResult

        return CompletionResult(content="我看到图片了", prompt_tokens=10, completion_tokens=5)

    from wecom_ai_gateway.providers import OpenAICompatibleProvider

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", fake_provider_complete)
    # 伪造下载：media_id → 假图片字节
    monkeypatch.setattr(
        svc.client,
        "download_media",
        AsyncMock(return_value=b"\x89PNG-fake-image-bytes"),
    )
    # 伪造发送
    send_text = AsyncMock(return_value="out-1")
    monkeypatch.setattr(svc.client, "send_text", send_text)

    await svc.process_message(row_id)

    assert len(calls) == 1, f"应只调用一次模型（直接走多模态），实际 {len(calls)}"
    base_url, model = calls[0]
    assert "deepseek.com" in base_url
    assert model == "deepseek-v4-flash-vision-exp"
    assert "sensenova" not in base_url
    send_text.assert_awaited_once()
    assert "我看到图片了" in send_text.await_args.args[2]


@pytest.mark.anyio
async def test_image_download_failure_falls_back_to_friendly_text(db, monkeypatch):
    """图片消息下载失败时给出友好提示，而不是当纯文本走主线路。"""
    from wecom_ai_gateway import services as svc

    monkeypatch.setattr(settings, "fallback_model", "deepseek-v4-flash-vision-exp")
    monkeypatch.setattr(settings, "fallback_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "fallback_api_key", "fallback-key")

    user_id, account_id = _setup_user(db)
    conversation = Conversation(user_id=user_id)
    db.add(conversation)
    db.flush()
    row = Message(
        user_id=user_id,
        conversation_id=conversation.id,
        channel="wecom_kf",
        channel_instance_id=account_id,
        external_message_id="img-fail-1",
        direction="inbound",
        message_type="image",
        content=None,
        status=MessageStatus.processing,
        metadata_json={"open_kfid": account_id, "media_types": ["image"], "media_count": 1},
    )
    db.add(row)
    db.flush()
    db.add(
        MediaAsset(
            message_id=row.id,
            channel="wecom_kf",
            media_type="image",
            mime="image/jpeg",
            storage_key="media-bad-id",
            status="stored",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    db.commit()
    db.close()

    # 下载失败（假 media_id）→ 应返回友好提示，且不调用任何模型
    from wecom_ai_gateway.providers import OpenAICompatibleProvider

    called = []
    monkeypatch.setattr(
        OpenAICompatibleProvider,
        "complete",
        lambda self, messages, model, temperature, max_tokens, **kw: called.append(model),
    )
    monkeypatch.setattr(svc.client, "download_media", AsyncMock(side_effect=RuntimeError("invalid media_id")))

    db = SessionLocal()
    row = db.get(Message, row.id)
    us = db.get(UserSettings, user_id)
    answer = await svc._complete_ai(db, row, conversation, us)
    db.close()

    assert "看不了" in answer
    assert called == [], "图片下载失败不应调用任何模型"


@pytest.mark.anyio
async def test_sequential_text_messages_are_merged_into_one_reply(db, monkeypatch):
    """同一用户短时间内连发多条消息，应合并成一次 AI 调用一起回复。"""
    from wecom_ai_gateway import services as svc

    user_id, account_id = _setup_user(db)
    conversation = Conversation(user_id=user_id)
    db.add(conversation)
    db.flush()
    rows = []
    for i, content in enumerate(["第一条", "第二条", "第三条"]):
        row = Message(
            user_id=user_id,
            conversation_id=conversation.id,
            channel="wecom_kf",
            channel_instance_id=account_id,
            external_message_id=f"merge-msg-{i}",
            direction="inbound",
            message_type="text",
            content=content,
            status=MessageStatus.queued,
            metadata_json={"open_kfid": account_id},
        )
        db.add(row)
        rows.append(row)
    db.commit()
    ids = [r.id for r in rows]
    db.close()

    # 捕获发给模型的 messages：验证三条消息都在同一次调用中
    captured = {}

    async def fake_provider_complete(self, messages, model, temperature, max_tokens, **kw):
        captured["messages"] = messages
        from wecom_ai_gateway.providers import CompletionResult

        return CompletionResult(content="合并回复", prompt_tokens=10, completion_tokens=5)

    from wecom_ai_gateway.providers import OpenAICompatibleProvider

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", fake_provider_complete)
    # 主线路直接成功（走平台默认 Sensenova 或 fallback 都行，重点是合并）
    send_text = AsyncMock(return_value="out-merge")
    monkeypatch.setattr(svc.client, "send_text", send_text)
    monkeypatch.setattr(
        svc,
        "resolve_provider",
        lambda db, us: ("openai-compatible", "https://primary.example/v1", "primary-key"),
    )

    # 处理第一条，应把 2、3 条一起合并回复
    await svc.process_message(ids[0])

    user_msgs = [
        m["content"]
        for m in captured.get("messages", [])
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    assert "第一条" in user_msgs[-1] or "第一条" in "\n".join(user_msgs)
    assert "第二条" in "\n".join(user_msgs)
    assert "第三条" in "\n".join(user_msgs)
    send_text.assert_awaited_once()

    # 第 2、3 条消息应被标记为已处理（不再单独回复）
    db = SessionLocal()
    for mid in ids[1:]:
        m = db.get(Message, mid)
        assert m.status == MessageStatus.sent, f"被合并消息 {mid} 应标记 sent，实际 {m.status}"
    db.close()


@pytest.mark.anyio
async def test_merged_messages_do_not_get_duplicate_reply(db, monkeypatch):
    """被合并的消息后续被 Outbox 处理时，不应再次回复。"""
    from wecom_ai_gateway import services as svc

    user_id, account_id = _setup_user(db)
    conversation = Conversation(user_id=user_id)
    db.add(conversation)
    db.flush()
    rows = []
    for i, content in enumerate(["甲", "乙"]):
        row = Message(
            user_id=user_id,
            conversation_id=conversation.id,
            channel="wecom_kf",
            channel_instance_id=account_id,
            external_message_id=f"dedupe-msg-{i}",
            direction="inbound",
            message_type="text",
            content=content,
            status=MessageStatus.queued,
            metadata_json={"open_kfid": account_id},
        )
        db.add(row)
        rows.append(row)
    db.commit()
    ids = [r.id for r in rows]
    db.close()

    send_text = AsyncMock(return_value="out")
    monkeypatch.setattr(svc.client, "send_text", send_text)
    from wecom_ai_gateway.providers import OpenAICompatibleProvider

    async def fake_complete(self, messages, model, temperature, max_tokens, **kw):
        return _fake_result()

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", fake_complete)
    monkeypatch.setattr(
        svc,
        "resolve_provider",
        lambda db, us: ("openai-compatible", "https://primary.example/v1", "primary-key"),
    )

    await svc.process_message(ids[0])  # 处理第一条，合并第二条
    send_text.reset_mock()
    await svc.process_message(ids[1])  # 第二条的 Outbox 任务再跑 → 应直接跳过
    send_text.assert_not_awaited()


def _fake_result():
    from wecom_ai_gateway.providers import CompletionResult

    return CompletionResult(content="ok", prompt_tokens=1, completion_tokens=1)
