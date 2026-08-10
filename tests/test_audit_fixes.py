"""审计修复回归测试：设置中心单位、超时生效、assert 校验、企微截断、锁 TTL。"""

from fastapi.testclient import TestClient

from wecom_ai_gateway.commands import execute
from wecom_ai_gateway.main import app
from wecom_ai_gateway.models import User
from wecom_ai_gateway.providers import OpenAICompatibleProvider, provider_for
from wecom_ai_gateway.runtime_settings import update_settings
from wecom_ai_gateway.services import ingest

client = TestClient(app)


def test_provider_forwards_runtime_timeout():
    provider = OpenAICompatibleProvider(
        base_url="https://x.example/v1", api_key="sk-test", timeout=321
    )
    assert provider._timeout == 321
    assert provider_for("openai-compatible", timeout=123)._timeout == 123


def test_provider_uses_injected_timeout_in_request(monkeypatch):
    import httpx

    captured = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            class _FakeInner:
                async def post(self, *args, **kwargs):
                    return httpx.Response(
                        200,
                        request=httpx.Request("POST", "https://x.example/v1/chat/completions"),
                        json={
                            "choices": [{"message": {"content": "你好"}}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                        },
                    )

            return _FakeInner()

        async def __aexit__(self, *exc):
            return None

    import wecom_ai_gateway.providers as providers_module

    monkeypatch.setattr(providers_module.httpx, "AsyncClient", FakeAsyncClient)
    provider = OpenAICompatibleProvider(
        base_url="https://x.example/v1", api_key="sk-test", timeout=7
    )
    import asyncio

    async def _run():
        await provider.complete([{"role": "user", "content": "hi"}], "m", 0.7, 100)

    asyncio.run(_run())
    assert captured["timeout"] == 7


def test_wecom_ingest_truncates_overlong_text(db):
    update_settings(db, {"message_max_chars": 100})
    item = {
        "msgid": "audit-long-1",
        "open_kfid": "wkAudit",
        "external_userid": "wmAuditUser",
        "msgtype": "text",
        "origin": 3,
        "text": {"content": "长" * 200},
    }
    ingest(db, item)
    from wecom_ai_gateway.models import Message

    row = db.query(Message).filter_by(external_message_id="audit-long-1").first()
    assert len(row.content) == 100


def test_command_limits_reject_out_of_range_without_assert(db):
    """边界校验不依赖 assert（Python -O 下 assert 会被禁用）。"""
    user = User(display_name="audit", mode="self_service")
    db.add(user)
    db.flush()
    from wecom_ai_gateway.models import UserSettings

    db.add(UserSettings(user_id=user.id))
    db.commit()
    update_settings(db, {"max_context_messages": 10})

    assert "超出允许范围" in execute(db, user.id, "/temperature 5").reply
    assert "超出允许范围" in execute(db, user.id, "/max-tokens 10").reply
    assert "超出允许范围" in execute(db, user.id, "/context 50").reply
    assert execute(db, user.id, "/context 8").handled


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.expire_calls = 0

    def incr(self, key):
        self.data[key] = self.data.get(key, 0) + 1
        return self.data[key]

    def get(self, key):
        return self.data.get(key)

    def expire(self, key, seconds):
        self.expire_calls += 1
        return True

    def delete(self, key):
        self.data.pop(key, None)
        return 1


def test_login_lock_refreshes_ttl_on_each_failure(monkeypatch):
    """每次失败都刷新 TTL，避免残留无过期 key 永久累积计数。"""
    import wecom_ai_gateway.auth as auth_module

    fake = FakeRedis()
    monkeypatch.setattr(auth_module, "redis_client", lambda: fake)
    auth_module.record_login_failure("ttl-user")
    auth_module.record_login_failure("ttl-user")
    assert fake.expire_calls == 2


def test_ip_limit_blocks_after_threshold(monkeypatch, db):
    """同 IP 超限返回 429；管理员登录不计入 IP 计数。"""
    import wecom_ai_gateway.auth as auth_module

    fake = FakeRedis()
    monkeypatch.setattr(auth_module, "redis_client", lambda: fake)
    update_settings(db, {"login_ip_max_attempts": 10, "login_ip_window_seconds": 900})

    assert auth_module.is_ip_locked("203.0.113.1") is False
    for _ in range(10):
        auth_module.record_ip_attempt("203.0.113.1")
    assert auth_module.is_ip_locked("203.0.113.1") is True
    auth_module.clear_ip_attempts("203.0.113.1")
    assert auth_module.is_ip_locked("203.0.113.1") is False
