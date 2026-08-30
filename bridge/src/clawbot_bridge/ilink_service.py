"""将 iLink HTTP 客户端适配为可停止、可续传的长轮询服务。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from .ilink import ILinkClient, ILinkCredentials, ParsedInboundText

log = logging.getLogger(__name__)


class ILinkService:
    def __init__(
        self,
        *,
        state_dir: Path,
        client_factory: Callable[[ILinkCredentials], ILinkClient] = ILinkClient,
        reconnect_delays: tuple[float, ...] = (1, 2, 5, 10, 30),
    ) -> None:
        self._state_dir = state_dir
        self._client_factory = client_factory
        if not reconnect_delays or any(delay < 0 for delay in reconnect_delays):
            raise ValueError("重连等待序列必须包含非负数")
        self._reconnect_delays = reconnect_delays

    def _cursor_path(self, instance_id: str) -> Path:
        safe_instance_id = instance_id.replace("/", "_").replace("..", "_")
        return self._state_dir / safe_instance_id / "get_updates_buf"

    def _load_cursor(self, instance_id: str) -> str:
        try:
            return self._cursor_path(instance_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _save_cursor(self, instance_id: str, cursor: str) -> None:
        cursor_path = self._cursor_path(instance_id)
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = cursor_path.with_suffix(".tmp")
        temp_path.write_text(cursor, encoding="utf-8")
        temp_path.replace(cursor_path)

    async def monitor(
        self,
        instance_id: str,
        credentials: ILinkCredentials,
        on_message: Callable[[ParsedInboundText], Awaitable[None]],
        stop_event: asyncio.Event,
    ) -> None:
        client = self._client_factory(credentials)
        cursor = self._load_cursor(instance_id)
        consecutive_failures = 0
        while not stop_event.is_set():
            try:
                next_cursor, messages = await client.get_updates(cursor)
            except (OSError, httpx.TransportError) as exc:
                delay = self._reconnect_delays[
                    min(consecutive_failures, len(self._reconnect_delays) - 1)
                ]
                consecutive_failures += 1
                log.warning(
                    "iLink transient connection failure instance=%s error=%s retry_in=%ss",
                    instance_id,
                    type(exc).__name__,
                    delay,
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except TimeoutError:
                    continue
                break
            consecutive_failures = 0
            for message in messages:
                await on_message(message)
            # 只有整批消息都被网关接受后，才提交 iLink 游标。
            self._save_cursor(instance_id, next_cursor)
            cursor = next_cursor

    async def send_text(
        self,
        credentials: ILinkCredentials,
        to_user_id: str,
        context_token: str,
        text: str,
    ) -> str:
        client = self._client_factory(credentials)
        return await client.send_text(
            to_user_id=to_user_id,
            context_token=context_token,
            text=text,
        )

    async def send_media(
        self,
        credentials: ILinkCredentials,
        to_user_id: str,
        context_token: str,
        media: dict,
    ) -> str:
        client = self._client_factory(credentials)
        return await client.send_media(
            to_user_id=to_user_id,
            context_token=context_token,
            media=media,
        )
