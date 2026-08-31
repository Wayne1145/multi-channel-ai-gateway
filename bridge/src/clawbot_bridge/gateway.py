"""向 Multi-Channel AI Gateway 转发规范化入站消息。"""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx


@dataclass(frozen=True)
class InboundTextMessage:
    sender_id: str
    external_message_id: str
    content: str
    media: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class GatewayClient:
    def __init__(self, *, base_url: str, bridge_token: str, timeout_seconds: float = 30.0) -> None:
        parsed = urlsplit(base_url)
        if parsed.query or parsed.fragment:
            raise ValueError("网关地址不得包含查询参数或片段")
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("网关地址无效")
        if not bridge_token:
            raise ValueError("bridge_token 不能为空")
        self._base_url = base_url.rstrip("/")
        self._bridge_token = bridge_token
        self._timeout_seconds = timeout_seconds

    async def forward(self, instance_id: str, message: InboundTextMessage) -> bool:
        url = f"{self._base_url}/api/internal/channel-instances/{instance_id}/messages"
        payload = {
            "sender_id": message.sender_id,
            "external_message_id": message.external_message_id,
            "message_type": "text",
            "content": message.content,
            "media": message.media,
        }
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self._bridge_token}"},
            )
            response.raise_for_status()
            data = response.json()
        return bool(data.get("accepted"))

    async def report_status(
        self,
        instance_id: str,
        *,
        status: str,
        account_id: str | None = None,
        qrcode_url: str | None = None,
        error: str | None = None,
    ) -> None:
        url = f"{self._base_url}/api/internal/channel-instances/{instance_id}/status"
        payload = {"status": status}
        if account_id:
            payload["account_id"] = account_id
        if qrcode_url:
            payload["qrcode_url"] = qrcode_url
        if error:
            payload["error"] = error
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self._bridge_token}"},
            )
            response.raise_for_status()
