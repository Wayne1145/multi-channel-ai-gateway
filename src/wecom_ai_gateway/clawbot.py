"""微信 ClawBot 桥接适配器。

本模块不内置个人微信协议，也不保存扫码凭据到日志。它通过受控 HTTP 桥接服务
与已部署的官方/上游 ClawBot Agent 交互；未配置桥接地址时实例保持 offline。
"""

from typing import Any
from urllib.parse import urlsplit

import httpx

from .channels import ChannelAdapter, OutgoingMessage
from .config import settings
from .redaction import redact_error


class ClawBotAdapter(ChannelAdapter):
    channel_key = "wechat_clawbot"

    def __init__(self, base_url: str | None = None) -> None:
        configured_url = base_url if base_url is not None else settings.clawbot_bridge_base_url
        parsed = urlsplit(configured_url)
        if parsed.query or parsed.fragment:
            raise ValueError("ClawBot 桥接地址不得包含查询参数或片段")
        self._base_url = configured_url.rstrip("/")

    def _url(self, path: str) -> str:
        if not self._base_url:
            raise RuntimeError("未配置 ClawBot 桥接服务")
        return f"{self._base_url}{path}"

    async def start_instance(self, instance_id: str) -> dict:
        """启动实例，并返回桥接侧的公开登录状态（如二维码地址）。"""
        return await self._post(f"/instances/{instance_id}/start", {})

    async def stop_instance(self, instance_id: str) -> None:
        await self._post(f"/instances/{instance_id}/stop", {})

    async def instance_status(self, instance_id: str) -> dict:
        """读取 Bridge 内存中的实时公开状态，并再次执行字段白名单。"""
        data = await self._request("GET", f"/instances/{instance_id}/status")
        return {
            key: value
            for key, value in data.items()
            if key in {"status", "qrcode_url", "account_id", "error"}
        }

    async def send(self, message: OutgoingMessage) -> str:
        data = await self._post(
            f"/instances/{message.instance_id}/messages",
            {
                "conversationId": message.to_sender_id,
                "text": message.text,
                "media": message.media,
                "metadata": message.metadata,
            },
        )
        message_id = data.get("messageId") or data.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise RuntimeError("ClawBot 桥接响应缺少消息 ID")
        return message_id

    async def send_media(self, message: OutgoingMessage) -> str | None:
        """ClawBot 桥接协议支持 media 字段，媒体走同一发送通道。"""
        if not message.media:
            return None
        return await self.send(message)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict:
        return await self._request("POST", path, payload)

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict:
        try:
            async with httpx.AsyncClient(timeout=settings.clawbot_request_timeout_seconds) as client:
                headers = (
                    {"Authorization": f"Bearer {settings.clawbot_bridge_token}"}
                    if settings.clawbot_bridge_token
                    else {}
                )
                response = await client.request(
                    method,
                    self._url(path),
                    json=payload if method != "GET" else None,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"ClawBot 桥接请求失败：{redact_error(exc)}") from exc
        if not isinstance(data, dict):
            raise TypeError("ClawBot 桥接响应格式无效")
        return data


def register_clawbot_adapter() -> None:
    """注册默认桥接适配器；重复导入时保持幂等。"""
    from .channels import registry

    try:
        registry.get(ClawBotAdapter.channel_key)
    except ValueError:
        registry.register(ClawBotAdapter())
