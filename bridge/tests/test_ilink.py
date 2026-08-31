import base64
import json
import logging

import httpx
import pytest
import respx

from clawbot_bridge.ilink import (
    ILinkClient,
    ILinkCredentials,
    SessionExpiredError,
    parse_inbound_text,
)


@pytest.mark.anyio
async def test_ilink_send_text_uses_exact_context_token() -> None:
    credentials = ILinkCredentials(
        bot_token="private-bot-token",
        base_url="https://ilinkai.weixin.qq.com",
    )
    client = ILinkClient(credentials)

    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://ilinkai.weixin.qq.com/ilink/bot/sendmessage").mock(
            return_value=httpx.Response(200, json={"ret": 0})
        )

        message_id = await client.send_text(
            to_user_id="user@im.wechat",
            context_token="exact-context-token",
            text="网关回复",
        )

    assert message_id.startswith("mcag-clawbot-")
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer private-bot-token"
    payload = json.loads(request.content)
    assert payload["msg"] == {
        "from_user_id": "",
        "to_user_id": "user@im.wechat",
        "client_id": message_id,
        "message_type": 2,
        "message_state": 2,
        "context_token": "exact-context-token",
        "item_list": [{"type": 1, "text_item": {"text": "网关回复"}}],
    }
    assert payload["base_info"]["channel_version"]


def test_parse_inbound_text_prefers_client_id_as_external_id() -> None:
    parsed = parse_inbound_text(
        {
            "from_user_id": "user@im.wechat",
            "client_id": "wechat-client-id-1",
            "message_id": 123,
            "message_type": 1,
            "context_token": "context-1",
            "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
        }
    )

    assert parsed is not None
    assert parsed.sender_id == "user@im.wechat"
    assert parsed.external_message_id == "wechat-client-id-1"
    assert parsed.context_token == "context-1"
    assert parsed.content == "你好"


def test_parse_inbound_text_ignores_non_user_or_empty_messages() -> None:
    assert parse_inbound_text({"message_type": 2, "item_list": []}) is None
    assert parse_inbound_text({"message_type": 1, "item_list": []}) is None


@pytest.mark.anyio
async def test_ilink_downloads_and_decrypts_inbound_pdf_without_exposing_cdn_credentials() -> None:
    key = b"0123456789abcdef"
    plaintext = b"%PDF-1.7 safe fixture"
    # iLink 文件密钥有时是 base64(32 位 hex 文本)，需要兼容两层编码。
    encoded_key = base64.b64encode(key.hex().encode()).decode()
    client = ILinkClient(ILinkCredentials("bot-secret", "https://ilinkai.weixin.qq.com"))
    encrypted = __import__("clawbot_bridge.ilink", fromlist=["_aes_ecb_encrypt"])._aes_ecb_encrypt(
        plaintext, key
    )
    raw = {
        "from_user_id": "user@im.wechat",
        "client_id": "file-message-1",
        "message_type": 1,
        "context_token": "context-file",
        "item_list": [
            {
                "type": 4,
                "file_item": {
                    "file_name": "guide.pdf",
                    "len": str(len(plaintext)),
                    "media": {
                        "encrypt_query_param": "must-not-leak-query",
                        "aes_key": encoded_key,
                    },
                },
            }
        ],
    }

    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://novac2c.cdn.weixin.qq.com/c2c/download",
            params={"encrypted_query_param": "must-not-leak-query"},
        ).mock(return_value=httpx.Response(200, content=encrypted))
        parsed = await client.parse_inbound(raw)

    assert parsed is not None
    assert parsed.content == ""
    assert parsed.media == [
        {
            "media_type": "file",
            "mime": "application/pdf",
            "filename": "guide.pdf",
            "size_bytes": len(plaintext),
            "data_base64": base64.b64encode(plaintext).decode(),
        }
    ]
    assert "must-not-leak-query" not in str(parsed.media)
    assert encoded_key not in str(parsed.media)


@pytest.mark.anyio
async def test_ilink_rejects_oversize_inbound_file_before_download() -> None:
    client = ILinkClient(ILinkCredentials("bot-secret", "https://ilinkai.weixin.qq.com"))
    raw = {
        "from_user_id": "user@im.wechat",
        "client_id": "file-message-large",
        "message_type": 1,
        "context_token": "context-file",
        "item_list": [
            {
                "type": 4,
                "file_item": {
                    "file_name": "huge.pdf",
                    "len": str(11 * 1024 * 1024),
                    "media": {
                        "encrypt_query_param": "large-query",
                        "aes_key": base64.b64encode(b"0123456789abcdef").decode(),
                    },
                },
            }
        ],
    }

    with respx.mock(assert_all_mocked=True) as router:
        parsed = await client.parse_inbound(raw)

    assert parsed is not None
    assert parsed.media[0]["status"] == "rejected"
    assert parsed.media[0]["rejected_reason"] == "size_exceeds_limit"
    assert not router.calls


@pytest.mark.anyio
async def test_ilink_login_fetches_qrcode_and_confirms_credentials() -> None:
    client = ILinkClient()
    with respx.mock(assert_all_called=True) as router:
        router.post(
            "https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"qrcode": "qr-secret", "qrcode_img_content": "https://qr.example/1"},
            )
        )
        router.get(
            "https://ilinkai.weixin.qq.com/ilink/bot/get_qrcode_status?qrcode=qr-secret"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "confirmed",
                    "bot_token": "bot-secret",
                    "ilink_bot_id": "bot@im.bot",
                    "baseurl": "https://ilink-node.weixin.qq.com",
                },
            )
        )

        pending = await client.start_login()
        success = await client.wait_login(pending)

    assert pending.qrcode_url == "https://qr.example/1"
    assert success.bot_token == "bot-secret"
    assert success.account_id == "bot@im.bot"
    assert success.base_url == "https://ilink-node.weixin.qq.com"


@pytest.mark.anyio
async def test_ilink_login_requests_qrcode_with_official_post_contract() -> None:
    client = ILinkClient()
    with respx.mock(assert_all_called=True) as router:
        route = router.post(
            "https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"qrcode": "qr-secret", "qrcode_img_content": "https://qr.example/1"},
            )
        )

        await client.start_login()

    request = route.calls[0].request
    assert json.loads(request.content) == {"local_token_list": []}
    assert request.headers["ilink-app-id"] == "bot"


@pytest.mark.anyio
async def test_ilink_login_failure_logs_only_safe_response_shape(caplog) -> None:
    client = ILinkClient()
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://ilinkai.weixin.qq.com/ilink/bot/get_qrcode_status?qrcode=qr-secret"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "confirmed",
                    "bot_token": "must-never-appear-in-logs",
                    "baseurl": "https://ilink-node.weixin.qq.com",
                },
            )
        )

        with (
            caplog.at_level(logging.ERROR, logger="clawbot_bridge.ilink"),
            pytest.raises(RuntimeError, match="ilink_bot_id"),
        ):
            await client.wait_login(
                type("Pending", (), {"session_key": "qr-secret"})()
            )

    assert "reason=missing_account_id" in caplog.text
    assert "status=confirmed" in caplog.text
    assert "fields=baseurl,bot_token,status" in caplog.text
    assert "must-never-appear-in-logs" not in caplog.text
    assert "qr-secret" not in caplog.text


@pytest.mark.anyio
async def test_ilink_get_updates_returns_parsed_messages_and_cursor() -> None:
    credentials = ILinkCredentials("bot-secret", "https://ilinkai.weixin.qq.com")
    client = ILinkClient(credentials)
    with respx.mock(assert_all_called=True) as router:
        router.post("https://ilinkai.weixin.qq.com/ilink/bot/getupdates").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ret": 0,
                    "get_updates_buf": "cursor-2",
                    "msgs": [
                        {
                            "message_type": 1,
                            "from_user_id": "user@im.wechat",
                            "client_id": "incoming-2",
                            "context_token": "context-2",
                            "item_list": [{"type": 1, "text_item": {"text": "第二条"}}],
                        }
                    ],
                },
            )
        )

        cursor, messages = await client.get_updates("cursor-1")

    assert cursor == "cursor-2"
    assert [message.external_message_id for message in messages] == ["incoming-2"]


@pytest.mark.anyio
async def test_ilink_get_updates_classifies_expired_session() -> None:
    client = ILinkClient(ILinkCredentials("bot-secret", "https://ilinkai.weixin.qq.com"))
    with respx.mock(assert_all_called=True) as router:
        router.post("https://ilinkai.weixin.qq.com/ilink/bot/getupdates").mock(
            return_value=httpx.Response(200, json={"ret": -14, "errcode": -14})
        )

        with pytest.raises(SessionExpiredError):
            await client.get_updates("cursor-1")
