from fastapi.testclient import TestClient

from clawbot_bridge.app import create_app


class FakeRuntime:
    """测试运行时：只记录 API 调用，不连接真实微信。"""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.sent: list[tuple[str, str, str]] = []
        self.restore_calls = 0

    async def restore_all(self) -> list[str]:
        self.restore_calls += 1
        return ["restored-instance"]

    async def start(self, instance_id: str) -> dict:
        self.started.append(instance_id)
        return {"status": "pending_login", "qrcode_url": "https://qr.example/1"}

    async def stop(self, instance_id: str) -> None:
        self.stopped.append(instance_id)

    async def send(
        self, instance_id: str, conversation_id: str, text: str, metadata: dict
    ) -> str:
        self.sent.append((instance_id, conversation_id, text, metadata))
        return "ilink-client-id-1"


def test_bridge_requires_bearer_token() -> None:
    client = TestClient(create_app(runtime=FakeRuntime(), bridge_token="bridge-secret"))

    response = client.post("/instances/instance-1/start", json={})

    assert response.status_code == 401


def test_bridge_restores_encrypted_sessions_during_startup() -> None:
    runtime = FakeRuntime()

    with TestClient(create_app(runtime=runtime, bridge_token="bridge-secret")):
        pass

    assert runtime.restore_calls == 1


def test_bridge_instance_lifecycle_and_message_contract() -> None:
    runtime = FakeRuntime()
    client = TestClient(create_app(runtime=runtime, bridge_token="bridge-secret"))
    headers = {"Authorization": "Bearer bridge-secret"}

    start = client.post("/instances/instance-1/start", json={}, headers=headers)
    sent = client.post(
        "/instances/instance-1/messages",
        headers=headers,
        json={
            "conversationId": "user@im.wechat",
            "text": "你好",
            "media": [],
            "metadata": {"reply_to": "incoming-1"},
        },
    )
    stop = client.post("/instances/instance-1/stop", json={}, headers=headers)

    assert start.status_code == 200
    assert start.json() == {
        "status": "pending_login",
        "qrcode_url": "https://qr.example/1",
    }
    assert sent.status_code == 200
    assert sent.json() == {"messageId": "ilink-client-id-1"}
    assert stop.status_code == 200
    assert runtime.started == ["instance-1"]
    assert runtime.sent == [
        ("instance-1", "user@im.wechat", "你好", {"reply_to": "incoming-1"})
    ]
    assert runtime.stopped == ["instance-1"]


def test_bridge_rejects_media_until_private_runtime_supports_it() -> None:
    client = TestClient(create_app(runtime=FakeRuntime(), bridge_token="bridge-secret"))

    response = client.post(
        "/instances/instance-1/messages",
        headers={"Authorization": "Bearer bridge-secret"},
        json={
            "conversationId": "user@im.wechat",
            "text": "",
            "media": [{"media_type": "image", "url": "https://example.com/a.png"}],
            "metadata": {},
        },
    )

    assert response.status_code == 501
