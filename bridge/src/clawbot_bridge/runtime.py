"""ClawBot 多实例运行时状态机。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from .gateway import GatewayClient, InboundTextMessage
from .ilink import ILinkCredentials, ParsedInboundText, SessionExpiredError
from .state import EncryptedStateStore, StoredInstanceState

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoginPending:
    session_key: str
    qrcode_url: str


@dataclass(frozen=True)
class LoginSuccess:
    bot_token: str
    base_url: str
    account_id: str


class LoginProvider(Protocol):
    async def start_login(self) -> LoginPending: ...

    async def wait_login(self, pending: LoginPending) -> LoginSuccess: ...


class ILinkRuntime(Protocol):
    async def monitor(
        self,
        instance_id: str,
        credentials: ILinkCredentials,
        on_message: Callable[[ParsedInboundText], Awaitable[None]],
        stop_event: asyncio.Event,
    ) -> None: ...

    async def send_text(
        self,
        credentials: ILinkCredentials,
        to_user_id: str,
        context_token: str,
        text: str,
    ) -> str: ...


@dataclass
class InstanceState:
    status: str
    pending: LoginPending
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    credentials: ILinkCredentials | None = None
    account_id: str | None = None
    context_tokens: dict[str, str] = field(default_factory=dict)
    task: asyncio.Task | None = None
    error: str | None = None


class BridgeRuntime:
    def __init__(
        self,
        *,
        login_provider: LoginProvider,
        gateway: GatewayClient,
        ilink: ILinkRuntime,
        state_store: EncryptedStateStore | None = None,
    ) -> None:
        self._login_provider = login_provider
        self._gateway = gateway
        self._ilink = ilink
        self._state_store = state_store
        self._instances: dict[str, InstanceState] = {}
        self._lock = asyncio.Lock()

    async def restore_all(self) -> list[str]:
        """进程启动时恢复数据卷中的全部有效加密会话。"""
        if not self._state_store:
            return []
        restored: list[str] = []
        for instance_id in self._state_store.instance_ids():
            result = await self.start(instance_id)
            if result.get("status") == "online":
                restored.append(instance_id)
        return restored

    async def start(self, instance_id: str) -> dict:
        async with self._lock:
            existing = self._instances.get(instance_id)
            if existing and existing.status in {"pending_login", "online"}:
                return self.status(instance_id)
            stored = self._state_store.load(instance_id) if self._state_store else None
            if stored:
                state = InstanceState(
                    status="online",
                    pending=LoginPending(session_key="restored", qrcode_url=""),
                    credentials=stored.credentials,
                    account_id=stored.account_id,
                    context_tokens=dict(stored.context_tokens),
                )
                self._instances[instance_id] = state
                state.task = asyncio.create_task(self._run_online_instance(instance_id, state))
                return {"status": "online", "account_id": stored.account_id}
            pending = await self._login_provider.start_login()
            state = InstanceState(status="pending_login", pending=pending)
            self._instances[instance_id] = state
            state.task = asyncio.create_task(self._run_instance(instance_id, state))
            return {"status": "pending_login", "qrcode_url": pending.qrcode_url}

    async def _run_instance(self, instance_id: str, state: InstanceState) -> None:
        try:
            login = await self._login_provider.wait_login(state.pending)
            if state.stop_event.is_set():
                return
            state.credentials = ILinkCredentials(
                bot_token=login.bot_token,
                base_url=login.base_url,
            )
            state.account_id = login.account_id
            state.status = "online"
            self._save_state(instance_id, state)
            await self._gateway.report_status(
                instance_id,
                status="online",
                account_id=login.account_id,
            )

            await self._monitor_online(instance_id, state)
        except asyncio.CancelledError:
            raise
        except SessionExpiredError:
            if self._state_store:
                self._state_store.clear(instance_id)
            pending = await self._login_provider.start_login()
            state.pending = pending
            state.credentials = None
            state.account_id = None
            state.context_tokens.clear()
            state.status = "pending_login"
            await self._gateway.report_status(
                instance_id,
                status="pending_login",
                qrcode_url=pending.qrcode_url,
            )
            state.task = asyncio.create_task(self._run_instance(instance_id, state))
        except Exception as exc:  # noqa: BLE001 - 后台任务必须把任意协议异常转换为可观察状态
            state.status = "error"
            state.error = type(exc).__name__
            log.error("ClawBot instance failed instance=%s error=%s", instance_id, type(exc).__name__)
            try:
                await self._gateway.report_status(instance_id, status="error")
            except Exception as report_exc:  # noqa: BLE001 - 回调失败不能覆盖原始实例错误
                log.error(
                    "Failed to report ClawBot status instance=%s error=%s",
                    instance_id,
                    type(report_exc).__name__,
                )
        finally:
            if state.status not in {"error", "pending_login"}:
                state.status = "offline"

    async def _run_online_instance(self, instance_id: str, state: InstanceState) -> None:
        try:
            await self._gateway.report_status(
                instance_id,
                status="online",
                account_id=state.account_id,
            )
            await self._monitor_online(instance_id, state)
        except SessionExpiredError:
            if self._state_store:
                self._state_store.clear(instance_id)
            pending = await self._login_provider.start_login()
            state.pending = pending
            state.credentials = None
            state.account_id = None
            state.context_tokens.clear()
            state.status = "pending_login"
            await self._gateway.report_status(
                instance_id,
                status="pending_login",
                qrcode_url=pending.qrcode_url,
            )
            state.task = asyncio.create_task(self._run_instance(instance_id, state))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务异常必须转换为可观察状态
            state.status = "error"
            state.error = type(exc).__name__
            log.error("Restored ClawBot instance failed instance=%s error=%s", instance_id, type(exc).__name__)

    async def _monitor_online(self, instance_id: str, state: InstanceState) -> None:
        if not state.credentials:
            raise RuntimeError("ClawBot credentials are missing")

        async def on_message(parsed: ParsedInboundText) -> None:
            state.context_tokens[parsed.external_message_id] = parsed.context_token
            self._save_state(instance_id, state)
            await self._gateway.forward(
                instance_id,
                InboundTextMessage(
                    sender_id=parsed.sender_id,
                    external_message_id=parsed.external_message_id,
                    content=parsed.content,
                    raw=parsed.raw,
                ),
            )

        await self._ilink.monitor(
            instance_id,
            state.credentials,
            on_message,
            state.stop_event,
        )

    def _save_state(self, instance_id: str, state: InstanceState) -> None:
        if not self._state_store or not state.credentials or not state.account_id:
            return
        self._state_store.save(
            instance_id,
            StoredInstanceState(
                credentials=state.credentials,
                account_id=state.account_id,
                context_tokens=dict(state.context_tokens),
            ),
        )

    async def stop(self, instance_id: str) -> None:
        async with self._lock:
            state = self._instances.get(instance_id)
            if not state:
                return
            state.stop_event.set()
            if state.task and not state.task.done():
                state.task.cancel()
                try:
                    await state.task
                except asyncio.CancelledError:
                    pass
            state.status = "offline"

    async def send(
        self,
        instance_id: str,
        conversation_id: str,
        text: str,
        metadata: dict,
    ) -> str:
        state = self._instances.get(instance_id)
        if not state or state.status != "online" or not state.credentials:
            raise RuntimeError("ClawBot instance is not online")
        reply_to = metadata.get("reply_to")
        context_token = state.context_tokens.get(str(reply_to)) if reply_to else None
        if not context_token:
            raise RuntimeError("找不到与 reply_to 匹配的 context_token")
        return await self._ilink.send_text(
            state.credentials,
            conversation_id,
            context_token,
            text,
        )

    def status(self, instance_id: str) -> dict:
        state = self._instances.get(instance_id)
        if not state:
            return {"status": "offline"}
        result: dict = {"status": state.status}
        if state.status == "pending_login":
            result["qrcode_url"] = state.pending.qrcode_url
        if state.account_id:
            result["account_id"] = state.account_id
        if state.error:
            result["error"] = state.error
        return result
