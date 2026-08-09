import asyncio
from pathlib import Path

import pytest

from clawbot_bridge.ilink import ILinkCredentials, ParsedInboundText
from clawbot_bridge.ilink_service import ILinkService


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.sent: list[tuple[str, str, str]] = []

    async def get_updates(self, cursor: str):
        self.calls.append(cursor)
        if len(self.calls) == 1:
            return (
                "cursor-2",
                [
                    ParsedInboundText(
                        sender_id="user@im.wechat",
                        external_message_id="incoming-1",
                        context_token="context-1",
                        content="你好",
                        raw={"message_type": 1},
                    )
                ],
            )
        await asyncio.sleep(0.02)
        return cursor, []

    async def send_text(self, *, to_user_id: str, context_token: str, text: str) -> str:
        self.sent.append((to_user_id, context_token, text))
        return "outbound-1"


@pytest.mark.anyio
async def test_ilink_service_persists_cursor_and_forwards_messages(tmp_path: Path) -> None:
    fake = FakeClient()
    service = ILinkService(
        state_dir=tmp_path,
        client_factory=lambda credentials: fake,
    )
    stop_event = asyncio.Event()
    received: list[str] = []

    async def on_message(message: ParsedInboundText) -> None:
        received.append(message.external_message_id)
        stop_event.set()

    await service.monitor(
        "instance-1",
        ILinkCredentials("secret", "https://ilinkai.weixin.qq.com"),
        on_message,
        stop_event,
    )

    assert received == ["incoming-1"]
    assert (tmp_path / "instance-1" / "get_updates_buf").read_text() == "cursor-2"


@pytest.mark.anyio
async def test_ilink_service_delegates_send_text(tmp_path: Path) -> None:
    fake = FakeClient()
    service = ILinkService(
        state_dir=tmp_path,
        client_factory=lambda credentials: fake,
    )

    message_id = await service.send_text(
        ILinkCredentials("secret", "https://ilinkai.weixin.qq.com"),
        "user@im.wechat",
        "context-1",
        "回复",
    )

    assert message_id == "outbound-1"
    assert fake.sent == [("user@im.wechat", "context-1", "回复")]
