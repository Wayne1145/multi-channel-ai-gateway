import json

import httpx
import pytest
import respx

from wecom_ai_gateway.channels import ChannelRegistry, OutgoingMessage
from wecom_ai_gateway.clawbot import ClawBotAdapter
from wecom_ai_gateway.config import settings


@pytest.mark.anyio
async def test_clawbot_adapter_sends_bridge_message():
    """桥接适配器只传递渠道字段，不接触用户数据库或登录凭据。"""
    adapter = ClawBotAdapter("https://bridge.example")
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://bridge.example/instances/instance-1/messages").mock(
            return_value=httpx.Response(200, json={"messageId": "bridge-message-1"})
        )
        message_id = await adapter.send(
            OutgoingMessage(
                channel="wechat_clawbot",
                instance_id="instance-1",
                to_sender_id="conversation-1",
                text="你好",
                metadata={"reply_to": "incoming-1"},
            )
        )

    assert message_id == "bridge-message-1"
    assert route.calls[0].request.headers.get("authorization") is None
    assert json.loads(route.calls[0].request.content) == {
        "conversationId": "conversation-1",
        "text": "你好",
        "media": [],
        "metadata": {"reply_to": "incoming-1"},
    }


@pytest.mark.anyio
async def test_clawbot_adapter_requires_bridge_configuration():
    adapter = ClawBotAdapter("")
    with pytest.raises(RuntimeError, match="未配置"):
        await adapter.start_instance("instance-1")


@pytest.mark.anyio
async def test_clawbot_adapter_uses_env_bridge_token_without_persisting_it(monkeypatch):
    monkeypatch.setattr(settings, "clawbot_bridge_token", "test-bridge-token")
    adapter = ClawBotAdapter("https://bridge.example")
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://bridge.example/instances/instance-1/start").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        await adapter.start_instance("instance-1")

    assert route.calls[0].request.headers["authorization"] == "Bearer test-bridge-token"


@pytest.mark.anyio
async def test_clawbot_adapter_reads_live_instance_status(monkeypatch):
    monkeypatch.setattr(settings, "clawbot_bridge_token", "test-bridge-token")
    adapter = ClawBotAdapter("https://bridge.example")
    with respx.mock(assert_all_called=True) as router:
        route = router.get("https://bridge.example/instances/instance-1/status").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "online",
                    "account_id": "bot@im.bot",
                    "bot_token": "must-never-be-returned",
                },
            )
        )
        status = await adapter.instance_status("instance-1")

    assert route.calls[0].request.headers["authorization"] == "Bearer test-bridge-token"
    assert status == {"status": "online", "account_id": "bot@im.bot"}
    assert "must-never-be-returned" not in str(status)


def test_clawbot_adapter_rejects_bridge_url_with_credentials():
    with pytest.raises(ValueError, match="不得包含查询参数"):
        ClawBotAdapter("https://bridge.example?access_token=never-leak")


def test_channel_registry_rejects_duplicate_adapter_registration():
    registry = ChannelRegistry()
    adapter = ClawBotAdapter("https://bridge.example")
    registry.register(adapter)

    assert registry.get("wechat_clawbot") is adapter
    assert registry.keys() == ("wechat_clawbot",)
    with pytest.raises(ValueError, match="已注册"):
        registry.register(adapter)
