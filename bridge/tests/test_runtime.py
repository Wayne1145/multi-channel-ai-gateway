import asyncio
from pathlib import Path

import pytest

from clawbot_bridge.gateway import GatewayClient
from clawbot_bridge.ilink import (
    ILinkCredentials,
    LoginQrExpiredError,
    ParsedInboundText,
    SessionExpiredError,
)
from clawbot_bridge.runtime import BridgeRuntime, LoginPending, LoginSuccess
from clawbot_bridge.state import EncryptedStateStore, StoredInstanceState


class FakeLoginProvider:
    def __init__(self) -> None:
        self.completed = asyncio.Event()

    async def start_login(self) -> LoginPending:
        return LoginPending(session_key="login-1", qrcode_url="https://qr.example/login-1")

    async def wait_login(self, pending: LoginPending) -> LoginSuccess:
        await self.completed.wait()
        return LoginSuccess(
            bot_token="private-token",
            base_url="https://ilinkai.weixin.qq.com",
            account_id="bot@im.bot",
        )


class FakeGateway(GatewayClient):
    def __init__(self) -> None:
        self.forwarded: list[tuple[str, object]] = []
        self.statuses: list[tuple[str, str, str | None]] = []

    async def forward(self, instance_id: str, message) -> bool:
        self.forwarded.append((instance_id, message))
        return True

    async def report_status(
        self,
        instance_id: str,
        *,
        status: str,
        account_id: str | None = None,
        qrcode_url: str | None = None,
        error: str | None = None,
    ) -> None:
        self.statuses.append((instance_id, status, account_id))


class FakeMonitor:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[ParsedInboundText] = asyncio.Queue()
        self.sent: list[tuple[str, str, str]] = []
        self.started = asyncio.Event()

    async def monitor(self, instance_id, credentials, on_message, stop_event) -> None:
        self.started.set()
        while not stop_event.is_set():
            try:
                parsed = await asyncio.wait_for(self.messages.get(), timeout=0.05)
            except TimeoutError:
                continue
            await on_message(parsed)

    async def send_text(self, credentials, to_user_id: str, context_token: str, text: str) -> str:
        self.sent.append((to_user_id, context_token, text))
        return "outbound-1"


@pytest.mark.anyio
async def test_runtime_start_returns_qrcode_without_waiting_for_scan() -> None:
    login = FakeLoginProvider()
    runtime = BridgeRuntime(
        login_provider=login,
        gateway=FakeGateway(),
        ilink=FakeMonitor(),
    )

    result = await runtime.start("instance-1")

    assert result == {
        "status": "pending_login",
        "qrcode_url": "https://qr.example/login-1",
    }
    assert runtime.status("instance-1")["status"] == "pending_login"
    await runtime.stop("instance-1")


@pytest.mark.anyio
async def test_runtime_forwards_inbound_and_replies_with_matching_context_token() -> None:
    login = FakeLoginProvider()
    gateway = FakeGateway()
    ilink = FakeMonitor()
    runtime = BridgeRuntime(login_provider=login, gateway=gateway, ilink=ilink)

    await runtime.start("instance-1")
    login.completed.set()
    await asyncio.wait_for(ilink.started.wait(), timeout=1)
    assert gateway.statuses == [("instance-1", "online", "bot@im.bot")]
    await ilink.messages.put(
        ParsedInboundText(
            sender_id="user@im.wechat",
            external_message_id="incoming-1",
            context_token="exact-context-1",
            content="你好",
            raw={"message_type": 1},
        )
    )
    await asyncio.sleep(0.1)

    assert gateway.forwarded[0][0] == "instance-1"
    assert gateway.forwarded[0][1].external_message_id == "incoming-1"
    message_id = await runtime.send(
        "instance-1",
        "user@im.wechat",
        "网关回复",
        {"reply_to": "incoming-1"},
    )

    assert message_id == "outbound-1"
    assert ilink.sent == [("user@im.wechat", "exact-context-1", "网关回复")]
    await runtime.stop("instance-1")


@pytest.mark.anyio
async def test_runtime_rejects_reply_without_known_context() -> None:
    login = FakeLoginProvider()
    ilink = FakeMonitor()
    runtime = BridgeRuntime(login_provider=login, gateway=FakeGateway(), ilink=ilink)

    await runtime.start("instance-1")
    login.completed.set()
    await asyncio.wait_for(ilink.started.wait(), timeout=1)

    with pytest.raises(RuntimeError, match="context_token"):
        await runtime.send(
            "instance-1",
            "user@im.wechat",
            "不能串发",
            {"reply_to": "unknown-message"},
        )
    await runtime.stop("instance-1")


@pytest.mark.anyio
async def test_runtime_reports_login_failure_without_sensitive_details() -> None:
    class FailingLogin(FakeLoginProvider):
        async def wait_login(self, pending: LoginPending) -> LoginSuccess:
            raise RuntimeError("login failed bot_token=must-never-leak")

    gateway = FakeGateway()
    runtime = BridgeRuntime(
        login_provider=FailingLogin(),
        gateway=gateway,
        ilink=FakeMonitor(),
    )

    await runtime.start("instance-error")
    await asyncio.sleep(0.05)

    assert gateway.statuses == [("instance-error", "error", None)]
    assert runtime.status("instance-error") == {
        "status": "error",
        "error": "RuntimeError",
    }


@pytest.mark.anyio
async def test_restored_runtime_reports_sanitized_error_status_to_gateway(tmp_path: Path) -> None:
    class FailingMonitor(FakeMonitor):
        async def monitor(self, instance_id, credentials, on_message, stop_event) -> None:
            raise RuntimeError("failed bot_token=must-never-leak")

    store = EncryptedStateStore(tmp_path, "bridge-secret-at-least-16")
    store.save(
        "instance-restored-error",
        StoredInstanceState(
            credentials=ILinkCredentials("stored-token", "https://ilinkai.weixin.qq.com"),
            account_id="stored-bot@im.bot",
            context_tokens={},
        ),
    )
    gateway = FakeGateway()
    runtime = BridgeRuntime(
        login_provider=FakeLoginProvider(),
        gateway=gateway,
        ilink=FailingMonitor(),
        state_store=store,
    )

    await runtime.start("instance-restored-error")
    await asyncio.sleep(0.05)

    assert runtime.status("instance-restored-error")["error"] == "RuntimeError"
    assert gateway.statuses[-1][:2] == ("instance-restored-error", "error")


@pytest.mark.anyio
async def test_restored_runtime_retries_initial_gateway_status_until_api_is_ready(tmp_path: Path) -> None:
    class StartingGateway(FakeGateway):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def report_status(self, *args, **kwargs) -> None:
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("api starting")
            await super().report_status(*args, **kwargs)

    store = EncryptedStateStore(tmp_path, "bridge-secret-at-least-16")
    store.save(
        "instance-startup-race",
        StoredInstanceState(
            credentials=ILinkCredentials("stored-token", "https://ilinkai.weixin.qq.com"),
            account_id="stored-bot@im.bot",
            context_tokens={},
        ),
    )
    gateway = StartingGateway()
    monitor = FakeMonitor()
    runtime = BridgeRuntime(
        login_provider=FakeLoginProvider(),
        gateway=gateway,
        ilink=monitor,
        state_store=store,
    )

    await runtime.start("instance-startup-race")
    await asyncio.wait_for(monitor.started.wait(), timeout=2)
    for _ in range(20):
        if gateway.calls >= 2:
            break
        await asyncio.sleep(0.05)

    assert gateway.calls >= 2
    assert runtime.status("instance-startup-race")["status"] == "online"
    assert gateway.statuses[-1][:2] == ("instance-startup-race", "online")
    await runtime.stop("instance-startup-race")


@pytest.mark.anyio
async def test_restored_runtime_monitors_even_when_gateway_status_callback_stays_down(
    tmp_path: Path,
) -> None:
    class DownGateway(FakeGateway):
        async def report_status(self, *args, **kwargs) -> None:
            raise ConnectionError("api unavailable")

    store = EncryptedStateStore(tmp_path, "bridge-secret-at-least-16")
    store.save(
        "instance-callback-down",
        StoredInstanceState(
            credentials=ILinkCredentials("stored-token", "https://ilinkai.weixin.qq.com"),
            account_id="stored-bot@im.bot",
            context_tokens={},
        ),
    )
    monitor = FakeMonitor()
    runtime = BridgeRuntime(
        login_provider=FakeLoginProvider(),
        gateway=DownGateway(),
        ilink=monitor,
        state_store=store,
    )

    await runtime.start("instance-callback-down")
    await asyncio.wait_for(monitor.started.wait(), timeout=0.5)

    assert runtime.status("instance-callback-down")["status"] == "online"
    await runtime.stop("instance-callback-down")


@pytest.mark.anyio
async def test_expired_restored_session_cancels_stale_online_report(tmp_path: Path) -> None:
    class RecordingDownGateway(FakeGateway):
        def __init__(self) -> None:
            super().__init__()
            self.online_attempts = 0

        async def report_status(self, instance_id: str, *, status: str, **kwargs) -> None:
            if status == "online":
                self.online_attempts += 1
                raise ConnectionError("api unavailable")
            await super().report_status(instance_id, status=status, **kwargs)

    class ExpiredMonitor(FakeMonitor):
        async def monitor(self, instance_id, credentials, on_message, stop_event) -> None:
            await asyncio.sleep(0.02)
            raise SessionExpiredError("expired")

    class WaitingLogin(FakeLoginProvider):
        async def wait_login(self, pending: LoginPending) -> LoginSuccess:
            await asyncio.Event().wait()

    store = EncryptedStateStore(tmp_path, "bridge-secret-at-least-16")
    store.save(
        "instance-stale-report",
        StoredInstanceState(
            credentials=ILinkCredentials("stored-token", "https://ilinkai.weixin.qq.com"),
            account_id="stored-bot@im.bot",
            context_tokens={},
        ),
    )
    gateway = RecordingDownGateway()
    runtime = BridgeRuntime(
        login_provider=WaitingLogin(),
        gateway=gateway,
        ilink=ExpiredMonitor(),
        state_store=store,
    )

    await runtime.start("instance-stale-report")
    await asyncio.sleep(0.2)
    attempts_after_rotation = gateway.online_attempts
    await asyncio.sleep(1.1)

    assert runtime.status("instance-stale-report")["status"] == "pending_login"
    assert gateway.online_attempts == attempts_after_rotation
    await runtime.stop("instance-stale-report")


@pytest.mark.anyio
async def test_runtime_session_expiry_returns_to_pending_login() -> None:
    class RotatingLogin(FakeLoginProvider):
        def __init__(self) -> None:
            super().__init__()
            self.starts = 0
            self.second_scan = asyncio.Event()

        async def start_login(self) -> LoginPending:
            self.starts += 1
            return LoginPending(
                session_key=f"login-{self.starts}",
                qrcode_url=f"https://qr.example/{self.starts}",
            )

        async def wait_login(self, pending: LoginPending) -> LoginSuccess:
            if pending.session_key == "login-2":
                await self.second_scan.wait()
            return LoginSuccess(
                bot_token=f"private-token-{pending.session_key}",
                base_url="https://ilinkai.weixin.qq.com",
                account_id="bot@im.bot",
            )

    class ExpiringMonitor(FakeMonitor):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def monitor(self, instance_id, credentials, on_message, stop_event) -> None:
            self.calls += 1
            if self.calls == 1:
                raise SessionExpiredError("expired")
            self.started.set()
            await stop_event.wait()

    login = RotatingLogin()
    gateway = FakeGateway()
    monitor = ExpiringMonitor()
    runtime = BridgeRuntime(login_provider=login, gateway=gateway, ilink=monitor)

    await runtime.start("instance-expired")
    login.completed.set()
    await asyncio.sleep(0.1)

    assert login.starts >= 2
    assert runtime.status("instance-expired") == {
        "status": "pending_login",
        "qrcode_url": "https://qr.example/2",
    }
    assert gateway.statuses[-1][:2] == ("instance-expired", "pending_login")
    login.second_scan.set()
    await asyncio.wait_for(monitor.started.wait(), timeout=1)
    assert runtime.status("instance-expired")["status"] == "online"
    assert monitor.calls == 2
    await runtime.stop("instance-expired")


@pytest.mark.anyio
async def test_runtime_rotates_expired_login_qrcode_without_entering_error() -> None:
    class ExpiringLogin(FakeLoginProvider):
        def __init__(self) -> None:
            super().__init__()
            self.starts = 0
            self.fresh_login = asyncio.Event()

        async def start_login(self) -> LoginPending:
            self.starts += 1
            return LoginPending(
                session_key=f"login-{self.starts}",
                qrcode_url=f"https://qr.example/{self.starts}",
            )

        async def wait_login(self, pending: LoginPending) -> LoginSuccess:
            if pending.session_key == "login-1":
                raise LoginQrExpiredError("expired")
            await self.fresh_login.wait()
            return LoginSuccess(
                bot_token="private-token",
                base_url="https://ilinkai.weixin.qq.com",
                account_id="bot@im.bot",
            )

    login = ExpiringLogin()
    gateway = FakeGateway()
    runtime = BridgeRuntime(
        login_provider=login,
        gateway=gateway,
        ilink=FakeMonitor(),
    )

    first = await runtime.start("instance-rotate")
    await asyncio.sleep(0.05)

    assert first["qrcode_url"] == "https://qr.example/1"
    assert login.starts == 2
    assert runtime.status("instance-rotate") == {
        "status": "pending_login",
        "qrcode_url": "https://qr.example/2",
    }
    assert gateway.statuses[-1][:2] == ("instance-rotate", "pending_login")
    assert all(status != "error" for _, status, _ in gateway.statuses)
    await runtime.stop("instance-rotate")


@pytest.mark.anyio
async def test_runtime_continues_waiting_for_rotated_qrcode_when_status_callback_fails() -> None:
    class ExpiringLogin(FakeLoginProvider):
        def __init__(self) -> None:
            super().__init__()
            self.starts = 0
            self.second_wait_started = asyncio.Event()

        async def start_login(self) -> LoginPending:
            self.starts += 1
            return LoginPending(
                session_key=f"login-{self.starts}", qrcode_url=f"https://qr.example/{self.starts}"
            )

        async def wait_login(self, pending: LoginPending) -> LoginSuccess:
            if pending.session_key == "login-1":
                raise LoginQrExpiredError("expired")
            self.second_wait_started.set()
            await asyncio.Event().wait()

    class DownGateway(FakeGateway):
        async def report_status(self, *args, **kwargs) -> None:
            raise ConnectionError("api unavailable")

    login = ExpiringLogin()
    runtime = BridgeRuntime(login_provider=login, gateway=DownGateway(), ilink=FakeMonitor())

    await runtime.start("instance-rotate-callback-down")
    await asyncio.wait_for(login.second_wait_started.wait(), timeout=0.5)

    assert runtime.status("instance-rotate-callback-down")["status"] == "pending_login"
    assert login.starts == 2
    await runtime.stop("instance-rotate-callback-down")


@pytest.mark.anyio
async def test_runtime_resumes_encrypted_session_without_new_qrcode(tmp_path: Path) -> None:
    login = FakeLoginProvider()
    gateway = FakeGateway()
    monitor = FakeMonitor()
    store = EncryptedStateStore(tmp_path, "bridge-secret-at-least-16")
    store.save(
        "instance-resume",
        StoredInstanceState(
            credentials=ILinkCredentials("stored-token", "https://ilinkai.weixin.qq.com"),
            account_id="stored-bot@im.bot",
            context_tokens={"incoming-old": "context-old"},
        ),
    )
    runtime = BridgeRuntime(
        login_provider=login,
        gateway=gateway,
        ilink=monitor,
        state_store=store,
    )

    result = await runtime.start("instance-resume")
    await asyncio.wait_for(monitor.started.wait(), timeout=1)

    assert result == {"status": "online", "account_id": "stored-bot@im.bot"}
    assert gateway.statuses == [("instance-resume", "online", "stored-bot@im.bot")]
    assert not login.completed.is_set()
    await runtime.stop("instance-resume")


@pytest.mark.anyio
async def test_runtime_restores_all_encrypted_sessions_at_process_start(tmp_path: Path) -> None:
    login = FakeLoginProvider()
    gateway = FakeGateway()
    monitor = FakeMonitor()
    store = EncryptedStateStore(tmp_path, "bridge-secret-at-least-16")
    store.save(
        "instance-restart",
        StoredInstanceState(
            credentials=ILinkCredentials("stored-token", "https://ilinkai.weixin.qq.com"),
            account_id="stored-bot@im.bot",
            context_tokens={},
        ),
    )
    runtime = BridgeRuntime(
        login_provider=login,
        gateway=gateway,
        ilink=monitor,
        state_store=store,
    )

    restored = await runtime.restore_all()
    await asyncio.wait_for(monitor.started.wait(), timeout=1)

    assert restored == ["instance-restart"]
    assert runtime.status("instance-restart") == {
        "status": "online",
        "account_id": "stored-bot@im.bot",
    }
    assert gateway.statuses == [("instance-restart", "online", "stored-bot@im.bot")]
    assert not login.completed.is_set()
    await runtime.stop("instance-restart")


@pytest.mark.anyio
async def test_runtime_does_not_restore_explicitly_stopped_instance(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, "bridge-secret-at-least-16")
    store.save(
        "instance-stopped",
        StoredInstanceState(
            credentials=ILinkCredentials("stored-token", "https://ilinkai.weixin.qq.com"),
            account_id="stored-bot@im.bot",
            context_tokens={},
        ),
    )
    running = BridgeRuntime(
        login_provider=FakeLoginProvider(),
        gateway=FakeGateway(),
        ilink=FakeMonitor(),
        state_store=store,
    )
    await running.start("instance-stopped")
    await running.stop("instance-stopped")

    restarted = BridgeRuntime(
        login_provider=FakeLoginProvider(),
        gateway=FakeGateway(),
        ilink=FakeMonitor(),
        state_store=store,
    )

    assert await restarted.restore_all() == []
    assert restarted.status("instance-stopped") == {"status": "offline"}


@pytest.mark.anyio
async def test_stop_marks_unloaded_persisted_instance_as_stopped(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, "bridge-secret-at-least-16")
    store.save(
        "instance-not-loaded",
        StoredInstanceState(
            credentials=ILinkCredentials("stored-token", "https://ilinkai.weixin.qq.com"),
            account_id="stored-bot@im.bot",
            context_tokens={},
        ),
    )
    runtime = BridgeRuntime(
        login_provider=FakeLoginProvider(),
        gateway=FakeGateway(),
        ilink=FakeMonitor(),
        state_store=store,
    )

    await runtime.stop("instance-not-loaded")

    assert store.load("instance-not-loaded").desired_running is False
    assert await runtime.restore_all() == []


@pytest.mark.anyio
async def test_explicit_start_resumes_previously_stopped_encrypted_session(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, "bridge-secret-at-least-16")
    store.save(
        "instance-paused",
        StoredInstanceState(
            credentials=ILinkCredentials("stored-token", "https://ilinkai.weixin.qq.com"),
            account_id="stored-bot@im.bot",
            context_tokens={},
            desired_running=False,
        ),
    )
    monitor = FakeMonitor()
    runtime = BridgeRuntime(
        login_provider=FakeLoginProvider(),
        gateway=FakeGateway(),
        ilink=monitor,
        state_store=store,
    )

    result = await runtime.start("instance-paused")
    await asyncio.wait_for(monitor.started.wait(), timeout=1)

    assert result == {"status": "online", "account_id": "stored-bot@im.bot"}
    assert store.load("instance-paused").desired_running is True
    await runtime.stop("instance-paused")
