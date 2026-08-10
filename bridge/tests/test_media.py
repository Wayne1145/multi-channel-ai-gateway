"""ClawBot Bridge 媒体收发测试：入站媒体解析、AES-128-ECB、CDN 上传、出站媒体消息。"""

import json

import httpx
import pytest
import respx

from clawbot_bridge.ilink import ILinkClient, ILinkCredentials, parse_inbound_text
from clawbot_bridge.runtime import BridgeRuntime


def test_parse_inbound_text_extracts_image_metadata():
    parsed = parse_inbound_text(
        {
            "message_type": 1,
            "from_user_id": "user@im.wechat",
            "client_id": "media-1",
            "context_token": "ctx-1",
            "item_list": [
                {
                    "type": 2,
                    "image_item": {
                        "media": {"full_url": "https://cdn.example/a.jpg", "encrypt_type": 1},
                        "mid_size": 1024,
                        "thumb_width": 100,
                    },
                }
            ],
        }
    )
    assert parsed is not None
    assert parsed.content == ""
    assert parsed.media == [
        {"media_type": "image", "mime": "", "filename": "", "size_bytes": 1024}
    ]


def test_parse_inbound_text_extracts_file_metadata():
    parsed = parse_inbound_text(
        {
            "message_type": 1,
            "from_user_id": "user@im.wechat",
            "client_id": "media-2",
            "context_token": "ctx-2",
            "item_list": [
                {
                    "type": 4,
                    "file_item": {
                        "file_name": "合同.pdf",
                        "len": "2048",
                        "md5": "abc123",
                    },
                }
            ],
        }
    )
    assert parsed is not None
    assert parsed.media == [
        {"media_type": "file", "mime": "", "filename": "合同.pdf", "size_bytes": 2048}
    ]


def test_parse_inbound_text_media_with_text_keeps_both():
    parsed = parse_inbound_text(
        {
            "message_type": 1,
            "from_user_id": "user@im.wechat",
            "client_id": "media-3",
            "context_token": "ctx-3",
            "item_list": [
                {"type": 1, "text_item": {"text": "看图"}},
                {"type": 2, "image_item": {"mid_size": 500}},
            ],
        }
    )
    assert parsed.content == "看图"
    assert len(parsed.media) == 1


def test_aes_ecb_roundtrip():
    from clawbot_bridge.ilink import _aes_ecb_decrypt, _aes_ecb_encrypt

    key = b"0123456789abcdef"
    ciphertext = _aes_ecb_encrypt(b"hello wechat media", key)
    assert len(ciphertext) % 16 == 0
    assert _aes_ecb_decrypt(ciphertext, key) == b"hello wechat media"


def test_parse_inbound_text_ignores_bot_messages_without_items():
    assert parse_inbound_text({"message_type": 1, "item_list": []}) is None


@pytest.mark.anyio
async def test_ilink_send_media_uploads_and_sends_image():
    credentials = ILinkCredentials("bot-token", "https://ilinkai.weixin.qq.com")
    client = ILinkClient(credentials)
    media = {"media_type": "image", "url": "https://files.example/a.png", "filename": "a.png"}

    with respx.mock(assert_all_called=True) as router:
        router.get("https://files.example/a.png").mock(
            return_value=httpx.Response(200, content=b"\x89PNG fake image data")
        )
        router.post("https://ilinkai.weixin.qq.com/ilink/bot/getuploadurl").mock(
            return_value=httpx.Response(
                200,
                json={"upload_full_url": "https://cdn.example/upload", "upload_param": None},
            )
        )
        router.post("https://cdn.example/upload").mock(
            return_value=httpx.Response(
                200, content=b"", headers={"x-encrypted-param": "download-param-1"}
            ),
        )
        route = router.post("https://ilinkai.weixin.qq.com/ilink/bot/sendmessage").mock(
            return_value=httpx.Response(200, json={"ret": 0})
        )

        message_id = await client.send_media(
            to_user_id="user@im.wechat",
            context_token="ctx-media",
            media=media,
        )

    assert message_id.startswith("mcag-clawbot-")
    request = route.calls[0].request
    payload = json.loads(request.content)
    item = payload["msg"]["item_list"][0]
    assert item["type"] == 2
    image_item = item["image_item"]
    assert image_item["media"]["encrypt_query_param"] == "download-param-1"
    assert image_item["media"]["encrypt_type"] == 1
    assert image_item["media"]["aes_key"]
    assert image_item["mid_size"] == len(b"\x89PNG fake image data")


@pytest.mark.anyio
async def test_ilink_send_media_rejects_insecure_download_url():
    credentials = ILinkCredentials("bot-token", "https://ilinkai.weixin.qq.com")
    client = ILinkClient(credentials)

    with pytest.raises(ValueError, match="https"):
        await client.send_media(
            to_user_id="user@im.wechat",
            context_token="ctx-media",
            media={"media_type": "image", "url": "http://insecure.example/a.png"},
        )


@pytest.mark.anyio
async def test_runtime_send_with_media_uses_exact_context_token():
    import tempfile
    from pathlib import Path

    from clawbot_bridge.gateway import GatewayClient
    from clawbot_bridge.state import EncryptedStateStore, StoredInstanceState

    class FakeMonitor:
        async def monitor(self, instance_id, credentials, on_message, stop_event):
            pass

        async def send_text(self, credentials, to_user_id, context_token, text):
            return "text-id"

        async def send_media(self, credentials, to_user_id, context_token, media):
            return "media-id"

    with tempfile.TemporaryDirectory() as tmp:
        store = EncryptedStateStore(Path(tmp), "bridge-secret-at-least-16")
        store.save(
            "instance-media",
            StoredInstanceState(
                credentials=ILinkCredentials("stored-token", "https://ilinkai.weixin.qq.com"),
                account_id="bot@im.bot",
                context_tokens={"incoming-9": "ctx-exact"},
            ),
        )
        runtime = BridgeRuntime(
            login_provider=None,
            gateway=GatewayClient(base_url="http://gw", bridge_token="bt"),
            ilink=FakeMonitor(),
            state_store=store,
        )
        await runtime.start("instance-media")

        sent = await runtime.send(
            "instance-media",
            "user@im.wechat",
            "",
            {"reply_to": "incoming-9", "media": [{"media_type": "image", "url": "https://x.example/a.png"}]},
        )

        assert sent == "media-id"
