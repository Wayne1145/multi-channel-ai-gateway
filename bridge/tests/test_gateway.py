import json

import httpx
import pytest
import respx

from clawbot_bridge.gateway import GatewayClient, InboundTextMessage


@pytest.mark.anyio
async def test_gateway_client_forwards_normalized_inbound_text() -> None:
    client = GatewayClient(
        base_url="http://gateway:8080",
        bridge_token="bridge-secret",
    )
    message = InboundTextMessage(
        sender_id="user@im.wechat",
        external_message_id="wechat-client-id-1",
        content="你好",
        raw={"message_type": 1},
    )

    with respx.mock(assert_all_called=True) as router:
        route = router.post(
            "http://gateway:8080/api/internal/channel-instances/instance-1/messages"
        ).mock(return_value=httpx.Response(200, json={"ok": True, "accepted": True}))

        result = await client.forward("instance-1", message)

    assert result is True
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer bridge-secret"
    assert json.loads(request.content) == {
        "sender_id": "user@im.wechat",
        "external_message_id": "wechat-client-id-1",
        "message_type": "text",
        "content": "你好",
        "media": [],
    }


@pytest.mark.anyio
async def test_gateway_client_forwards_decrypted_file_only_over_internal_bearer() -> None:
    client = GatewayClient(base_url="http://gateway:8080", bridge_token="bridge-secret")
    message = InboundTextMessage(
        sender_id="user@im.wechat",
        external_message_id="file-1",
        content="",
        media=[
            {
                "media_type": "file",
                "mime": "application/pdf",
                "filename": "guide.pdf",
                "size_bytes": 4,
                "data_base64": "JVBERg==",
            }
        ],
    )
    with respx.mock(assert_all_called=True) as router:
        route = router.post(
            "http://gateway:8080/api/internal/channel-instances/instance-1/messages"
        ).mock(return_value=httpx.Response(200, json={"accepted": True}))
        assert await client.forward("instance-1", message) is True

    payload = json.loads(route.calls[0].request.content)
    assert route.calls[0].request.headers["authorization"] == "Bearer bridge-secret"
    assert payload["media"][0]["data_base64"] == "JVBERg=="
    assert "raw" not in payload


def test_gateway_client_rejects_url_with_query_credentials() -> None:
    with pytest.raises(ValueError, match="查询参数"):
        GatewayClient(
            base_url="https://gateway.example?token=never",
            bridge_token="bridge-secret",
        )


@pytest.mark.anyio
async def test_gateway_client_reports_safe_instance_status() -> None:
    client = GatewayClient(
        base_url="http://gateway:8080",
        bridge_token="bridge-secret",
    )
    with respx.mock(assert_all_called=True) as router:
        route = router.post(
            "http://gateway:8080/api/internal/channel-instances/instance-1/status"
        ).mock(return_value=httpx.Response(200, json={"status": "online"}))

        await client.report_status(
            "instance-1",
            status="online",
            account_id="bot@im.bot",
        )

    assert json.loads(route.calls[0].request.content) == {
        "status": "online",
        "account_id": "bot@im.bot",
    }


@pytest.mark.anyio
async def test_gateway_client_reports_pending_login_qrcode() -> None:
    client = GatewayClient(
        base_url="http://gateway:8080",
        bridge_token="bridge-secret",
    )
    with respx.mock(assert_all_called=True) as router:
        route = router.post(
            "http://gateway:8080/api/internal/channel-instances/instance-1/status"
        ).mock(return_value=httpx.Response(200, json={"status": "logging_in"}))

        await client.report_status(
            "instance-1",
            status="pending_login",
            qrcode_url="https://qr.example/reconnect",
        )

    assert json.loads(route.calls[0].request.content) == {
        "status": "pending_login",
        "qrcode_url": "https://qr.example/reconnect",
    }
