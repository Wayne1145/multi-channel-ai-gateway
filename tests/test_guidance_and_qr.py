"""0014 迁移 + 命令指引开关 + /qr clawbot 动态人设化 的回归测试。"""

import sys
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from wecom_ai_gateway.clawbot import ClawBotAdapter
from wecom_ai_gateway.config import settings
from wecom_ai_gateway.db import SessionLocal
from wecom_ai_gateway.main import app
from wecom_ai_gateway.models import (
    Account,
    ChannelIdentity,
    CharacterCard,
    Conversation,
    Message,
    MessageDirection,
    MessageStatus,
    User,
    UserSettings,
)
from wecom_ai_gateway.security import encrypt_secret

client = TestClient(app)


def _login_user(username: str = "guidance_user", password: str = "guidance-pass") -> tuple[str, str]:
    from wecom_ai_gateway.security import hash_password

    db = SessionLocal()
    user = User(display_name="GuidanceUser", mode="self_service")
    db.add(user)
    db.flush()
    db.add(
        Account(
            user_id=user.id,
            username=username,
            password_hash=hash_password(password),
            role="user",
        )
    )
    db.commit()
    user_id = user.id
    db.close()
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["token"]
    return user_id, token


def _install_fake_qrcode(monkeypatch):
    """本地测试环境可能未装 qrcode；用一个伪模块让 services.py 的延迟 import 通过。"""
    class _FakeImg:
        def save(self, fp, **kw):
            fp.write(b"\x89PNG\r\n\x1a\n" + b"FAKE-QR-PIXELS" * 20)

    def _fake_qr_make(*a, **kw):
        return _FakeImg()

    fake_mod = type(sys)("qrcode")
    fake_mod.make = _fake_qr_make
    monkeypatch.setitem(sys.modules, "qrcode", fake_mod)


def test_me_summary_exposes_command_guidance_enabled_default_true():
    _, token = _login_user()
    resp = client.get("/api/me/summary", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["command_guidance_enabled"] is True


def test_patch_me_settings_can_toggle_command_guidance():
    _, token = _login_user()
    off = client.patch(
        "/api/me/settings",
        headers={"Authorization": f"Bearer {token}"},
        json={"command_guidance_enabled": False},
    )
    assert off.status_code == 200
    assert off.json()["command_guidance_enabled"] is False
    on = client.patch(
        "/api/me/settings",
        headers={"Authorization": f"Bearer {token}"},
        json={"command_guidance_enabled": True},
    )
    assert on.status_code == 200
    assert on.json()["command_guidance_enabled"] is True
    assert client.get("/api/me/summary", headers={"Authorization": f"Bearer {token}"}).json()[
        "command_guidance_enabled"
    ] is True


def test_patch_me_settings_rejects_non_boolean():
    _, token = _login_user()
    bad = client.patch(
        "/api/me/settings",
        headers={"Authorization": f"Bearer {token}"},
        json={"command_guidance_enabled": "yes"},
    )
    assert bad.status_code == 400


@pytest.mark.anyio
async def test_complete_ai_injects_help_only_when_guidance_enabled(monkeypatch):
    """命令索引只在 command_guidance_enabled=True 时注入 system prompt。"""
    from wecom_ai_gateway import services as svc
    from wecom_ai_gateway.commands import HELP as COMMAND_HELP

    db = SessionLocal()
    user = User()
    db.add(user)
    db.flush()
    us = UserSettings(user_id=user.id, command_guidance_enabled=True)
    conv = Conversation(user_id=user.id)
    row = Message(
        user_id=user.id,
        conversation_id=conv.id,
        channel="wecom_kf",
        external_message_id="msg-guid-1",
        direction=MessageDirection.inbound,
        message_type="text",
        content="你好",
        status=MessageStatus.processing,
    )
    db.add_all([us, conv, row])
    db.commit()

    captured = []

    async def fake_complete_with_routing(db_arg, us_arg, prompts, **kw):
        captured.extend(prompts)
        from wecom_ai_gateway.model_routing import RoutedCompletion

        return RoutedCompletion(
            content="hi",
            provider_name="fake",
            provider_key="fake",
            model="fake",
            prompt_tokens=1,
            completion_tokens=1,
            tool_calls=[],
        )

    mr_module = sys.modules["wecom_ai_gateway.model_routing"]
    monkeypatch.setattr(mr_module, "complete_with_routing", fake_complete_with_routing)
    monkeypatch.setattr(
        mr_module,
        "active_routes",
        lambda db_arg, gid=None: [{"provider_id": "p1", "model": "m1", "priority": 10, "enabled": True}],
    )

    await svc._complete_ai(db, row, conv, us)
    sys_text_on = next(p["content"] for p in captured if p.get("role") == "system")
    assert "/help" in sys_text_on
    assert "发送 /help 即可查看完整命令列表" in sys_text_on
    assert COMMAND_HELP in sys_text_on

    captured.clear()
    us.command_guidance_enabled = False
    db.commit()
    await svc._complete_ai(db, row, conv, us)
    sys_text_off = next(p["content"] for p in captured if p.get("role") == "system")
    assert "/help" not in sys_text_off
    assert COMMAND_HELP not in sys_text_off
    db.close()


@pytest.mark.anyio
async def test_qr_clawbot_text_uses_persona_name(monkeypatch):
    """/qr clawbot 的提示文案应带用户当前角色卡名称。"""
    from wecom_ai_gateway import services as svc

    _install_fake_qrcode(monkeypatch)
    monkeypatch.setattr(settings, "clawbot_bridge_base_url", "https://bridge.test")
    monkeypatch.setattr(settings, "wecom_open_kfid", "wk-test")
    monkeypatch.setattr(settings, "wecom_corp_id", "wwCorp")

    async def fake_start_persona(*args, **kwargs):
        return {"status": "pending_login", "qrcode_url": "https://qr.example/abc"}

    monkeypatch.setattr(ClawBotAdapter, "start_instance", fake_start_persona)

    db = SessionLocal()
    user = User()
    db.add(user)
    db.flush()
    card = CharacterCard(user_id=user.id, name="小月", content_encrypted="encrypted-test")
    db.add(card)
    db.flush()
    us = UserSettings(user_id=user.id, active_card_id=card.id)
    db.add(us)
    db.commit()

    conv = Conversation(user_id=user.id)
    db.add(conv)
    db.commit()
    row = Message(
        user_id=user.id,
        conversation_id=conv.id,
        channel="wecom_kf",
        external_message_id="msg-qr-1",
        direction="inbound",
        message_type="text",
        content="/qr clawbot",
        status=MessageStatus.processing,
        metadata_json={"open_kfid": "wk-test"},
    )
    db.add(row)
    db.commit()

    payload = await svc._handle_qr_clawbot_command(db, row)
    assert payload is not None
    assert payload["text"].startswith("小月：")
    assert "/qr clawbot" in payload["text"]
    assert isinstance(payload.get("media_bytes"), bytes)
    assert len(payload["media_bytes"]) > 100
    db.close()


@pytest.mark.anyio
async def test_qr_clawbot_falls_back_to_default_greeting_without_persona(monkeypatch):
    """无角色卡时使用默认'八千代'问候，保证不报异常。"""
    from wecom_ai_gateway import services as svc

    _install_fake_qrcode(monkeypatch)
    monkeypatch.setattr(settings, "clawbot_bridge_base_url", "https://bridge.test")
    monkeypatch.setattr(settings, "wecom_open_kfid", "wk-test")
    monkeypatch.setattr(settings, "wecom_corp_id", "wwCorp")

    async def fake_start_fallback(*args, **kwargs):
        return {"status": "pending_login", "qrcode_url": "https://qr.example/abc"}

    monkeypatch.setattr(ClawBotAdapter, "start_instance", fake_start_fallback)

    db = SessionLocal()
    user = User()
    db.add(user)
    db.flush()
    us = UserSettings(user_id=user.id)
    db.add_all([us])
    db.commit()

    conv = Conversation(user_id=user.id)
    db.add(conv)
    db.commit()
    row = Message(
        user_id=user.id,
        conversation_id=conv.id,
        channel="wecom_kf",
        external_message_id="msg-qr-fallback",
        direction="inbound",
        message_type="text",
        content="/qr clawbot",
        status=MessageStatus.processing,
        metadata_json={"open_kfid": "wk-test"},
    )
    db.add(row)
    db.commit()

    payload = await svc._handle_qr_clawbot_command(db, row)
    assert payload is not None
    assert payload["text"].startswith("八千代：")
    db.close()


@pytest.mark.anyio
async def test_qr_clawbot_process_message_sends_text_and_media(monkeypatch):
    """/qr clawbot 走 process_message 时必须真正调用 send_text 与 send_media。

    回归背景：旧实现只写库不投递，用户在企微收不到文本回执；同时生产容器缺
    Pillow 导致二维码图片生成失败（'No module named PIL'）。这里同时验证
    投递链路与图片生成容错。
    """
    from unittest.mock import AsyncMock

    from wecom_ai_gateway import services as svc

    _install_fake_qrcode(monkeypatch)
    monkeypatch.setattr(settings, "clawbot_bridge_base_url", "https://bridge.test")
    monkeypatch.setattr(settings, "wecom_open_kfid", "wk-test")
    monkeypatch.setattr(settings, "wecom_corp_id", "wwCorp")
    # 桥接启动返回二维码地址
    async def fake_start_any(*args, **kwargs):
        return {"status": "pending_login", "qrcode_url": "https://qr.example/abc"}

    monkeypatch.setattr(ClawBotAdapter, "start_instance", fake_start_any)

    send_text = AsyncMock(return_value="out-msg-1")
    send_media = AsyncMock(return_value="out-media-1")
    upload = AsyncMock(return_value="media-id-1")

    db = SessionLocal()
    user = User()
    db.add(user)
    db.flush()
    conv = Conversation(user_id=user.id)
    identity = ChannelIdentity(
        user_id=user.id,
        channel="wecom_kf",
        account_id="wk-test",
        external_id_hash="a" * 64,
        external_id_encrypted=encrypt_secret("external-user"),
    )
    row = Message(
        user_id=user.id,
        conversation_id=conv.id,
        channel="wecom_kf",
        external_message_id="msg-qr-deliver",
        direction="inbound",
        message_type="text",
        content="/qr clawbot",
        status=MessageStatus.queued,
        metadata_json={"open_kfid": "wk-test"},
    )
    db.add_all([conv, identity, row])
    db.commit()
    db.close()

    with (
        patch.object(svc.client, "send_text", send_text),
        patch.object(svc.client, "send_media", send_media),
        patch.object(svc.client, "upload_media_from_bytes", upload),
    ):
        await svc.process_message(row.id)

    send_text.assert_awaited_once()
    assert "八千代：已为你生成微信登录二维码" in send_text.await_args.args[2]
    upload.assert_awaited_once()
    send_media.assert_awaited_once()

    db = SessionLocal()
    outbound = (
        db.query(Message)
        .filter(
            Message.channel == "wecom_kf",
            Message.direction == MessageDirection.outbound,
            Message.metadata_json.like("%qr%"),
        )
        .all()
    )
    assert outbound, "qr 回复必须写库"
    statuses = {m.message_type for m in outbound}
    assert "text" in statuses and "image" in statuses
    db.close()


@pytest.mark.anyio
async def test_qr_clawbot_returns_degraded_text_when_bridge_missing(monkeypatch):
    """桥接未配置时返回降级文本，不抛异常。"""
    from wecom_ai_gateway import services as svc

    monkeypatch.setattr(settings, "clawbot_bridge_base_url", "")
    monkeypatch.setattr(settings, "wecom_open_kfid", "wk-test")
    monkeypatch.setattr(settings, "wecom_corp_id", "wwCorp")

    db = SessionLocal()
    user = User()
    db.add(user)
    db.flush()
    conv = Conversation(user_id=user.id)
    db.add_all([conv])
    db.commit()
    row = Message(
        user_id=user.id,
        conversation_id=conv.id,
        channel="wecom_kf",
        external_message_id="msg-qr-bad",
        direction="inbound",
        message_type="text",
        content="/qr clawbot",
        status=MessageStatus.processing,
        metadata_json={"open_kfid": "wk-test"},
    )
    db.add(row)
    db.commit()
    payload = await svc._handle_qr_clawbot_command(db, row)
    assert payload is not None
    assert "未配置" in payload["text"]
    assert payload.get("media_bytes") is None
    db.close()