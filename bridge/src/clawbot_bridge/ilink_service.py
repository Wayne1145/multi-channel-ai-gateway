"""将 iLink HTTP 客户端适配为可停止、可续传的长轮询服务。"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from .ilink import ILinkClient, ILinkCredentials, ParsedInboundText


class ILinkService:
    def __init__(
        self,
        *,
        state_dir: Path,
        client_factory: Callable[[ILinkCredentials], ILinkClient] = ILinkClient,
    ) -> None:
        self._state_dir = state_dir
        self._client_factory = client_factory

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
        while not stop_event.is_set():
            cursor, messages = await client.get_updates(cursor)
            self._save_cursor(instance_id, cursor)
            for message in messages:
                await on_message(message)
                if stop_event.is_set():
                    break

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
