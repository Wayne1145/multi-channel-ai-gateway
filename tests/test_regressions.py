"""审计缺陷的回归测试。

这些测试固定既有产品语义：多用户数据隔离、统一管理模式、精确渠道回复、
可靠任务租约和敏感异常脱敏。修复不得通过削减原功能来让测试通过。
"""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from wecom_ai_gateway.commands import execute
from wecom_ai_gateway.models import (
    ChannelIdentity,
    CharacterCard,
    Conversation,
    Message,
    MessageStatus,
    OutboxStatus,
    OutboxTask,
    PlatformConfig,
    User,
    UserProvider,
    UserSettings,
)
from wecom_ai_gateway.policy import get_command_decision
from wecom_ai_gateway.presets import apply_snapshot
from wecom_ai_gateway.security import encrypt_secret
from wecom_ai_gateway.services import process_message, resolve_provider
from wecom_ai_gateway.tasks import claim_task, complete_task, fail_task, utcnow
from wecom_ai_gateway.wecom import WeComClient


def _user_with_settings(db):
    user = User()
    db.add(user)
    db.flush()
    settings = UserSettings(user_id=user.id)
    db.add(settings)
    db.flush()
    return user, settings


def _inbound(db, user, account_id, external_id, msgid, text="你好"):
    identity = db.query(ChannelIdentity).filter_by(
        user_id=user.id, channel="wecom_kf", account_id=account_id
    ).one_or_none()
    if identity is None:
        identity = ChannelIdentity(
            user_id=user.id,
            channel="wecom_kf",
            account_id=account_id,
            external_id_hash=(account_id + external_id).encode().hex()[:64].ljust(64, "0"),
            external_id_encrypted=encrypt_secret(external_id),
        )
    conversation = Conversation(user_id=user.id)
    db.add_all([identity, conversation])
    db.flush()
    message = Message(
        user_id=user.id,
        conversation_id=conversation.id,
        channel="wecom_kf",
        external_message_id=msgid,
        direction="inbound",
        message_type="text",
        content=text,
        status=MessageStatus.queued,
        metadata_json={"open_kfid": account_id},
    )
    db.add(message)
    db.commit()
    return message


@pytest.mark.anyio
async def test_reply_uses_identity_from_inbound_account(db):
    user, _ = _user_with_settings(db)
    _inbound(db, user, "account-a", "external-a", "old-a")
    message = _inbound(db, user, "account-b", "external-b", "from-b", "/status")
    send = AsyncMock(return_value="reply-b")

    with patch("wecom_ai_gateway.services.client.send_text", send):
        await process_message(message.id)

    assert send.await_args.args[:2] == ("account-b", "external-b")


@pytest.mark.anyio
async def test_unknown_prior_reply_dispatch_is_not_sent_twice(db):
    user, _ = _user_with_settings(db)
    message = _inbound(db, user, "account-a", "external-a", "already-dispatched", "/status")
    message.metadata_json = {"open_kfid": "account-a", "reply_dispatch": "started"}
    db.commit()
    send = AsyncMock(return_value="should-not-send")

    with (
        patch("wecom_ai_gateway.services.client.send_text", send),
        pytest.raises(RuntimeError, match="停止自动重发"),
    ):
        await process_message(message.id)

    send.assert_not_awaited()
    db.expire_all()
    assert db.get(Message, message.id).status == MessageStatus.failed


def test_byok_provider_must_belong_to_requesting_user(db):
    owner, _ = _user_with_settings(db)
    _requester, requester_settings = _user_with_settings(db)
    provider = UserProvider(
        user_id=owner.id,
        provider_key="openai-compatible",
        base_url="https://owner.invalid/v1",
        api_key_encrypted=encrypt_secret("owner-secret"),
    )
    db.add(provider)
    db.flush()
    requester_settings.provider_key = f"byok:{provider.id}"

    with (
        patch("wecom_ai_gateway.services.settings.openai_compatible_api_key", "platform-secret"),
        pytest.raises(RuntimeError, match="BYOK 配置已失效"),
    ):
        resolve_provider(db, requester_settings)


def test_preset_cannot_activate_another_users_card(db):
    user, settings = _user_with_settings(db)
    other, _ = _user_with_settings(db)
    own_card = CharacterCard(user_id=user.id, name="自己的卡")
    other_card = CharacterCard(user_id=other.id, name="别人的卡")
    db.add_all([own_card, other_card])
    db.flush()
    settings.active_card_id = own_card.id

    apply_snapshot(db, settings, {"active_card_id": other_card.id})

    assert settings.active_card_id == own_card.id


def test_active_card_lookup_rejects_cross_user_reference(db):
    user, settings = _user_with_settings(db)
    other, _ = _user_with_settings(db)
    other_card = CharacterCard(user_id=other.id, name="别人的卡", content_encrypted=encrypt_secret("秘密"))
    db.add(other_card)
    db.flush()
    settings.active_card_id = other_card.id

    result = execute(db, user.id, "/status")

    assert "角色卡：无" in result.reply


def test_managed_mode_blocks_user_configuration_by_default(db):
    user, _ = _user_with_settings(db)
    db.add(PlatformConfig(key="mode", value={"mode": "managed"}))
    db.flush()

    decision = get_command_decision(db, user.id, "wecom_kf", "card")
    safe_decision = get_command_decision(db, user.id, "wecom_kf", "status")

    assert decision.allowed is False
    assert decision.silent_block is True
    assert decision.blocked_strategy == "redirect_to_ai"
    assert safe_decision.allowed is True


def test_single_mode_still_allows_commands(db):
    user, _ = _user_with_settings(db)
    db.add(PlatformConfig(key="mode", value={"mode": "managed"}))
    db.flush()
    with patch("wecom_ai_gateway.policy.settings.single_user_mode", True):
        assert get_command_decision(db, user.id, "wecom_kf", "card").allowed is True


@pytest.mark.anyio
async def test_clear_keeps_current_command_sendable_and_removes_older_history(db):
    user, _ = _user_with_settings(db)
    old = _inbound(db, user, "account-a", "external-a", "old-history")
    current = _inbound(db, user, "account-a", "external-a", "clear-now", "/clear confirm")
    old_id = old.id
    current_id = current.id
    send = AsyncMock(return_value="clear-reply")

    with patch("wecom_ai_gateway.services.client.send_text", send):
        await process_message(current.id)

    db.expire_all()
    assert db.get(Message, old_id) is None
    assert db.get(Message, current_id).status == MessageStatus.sent
    assert db.query(Message).filter_by(external_message_id="clear-reply").one().status == MessageStatus.sent


def test_stale_worker_cannot_overwrite_new_lease_result(db):
    task = OutboxTask(task_type="message", dedupe_key="lease:1", payload={"message_id": "x"})
    db.add(task)
    db.commit()
    first = claim_task()
    assert first.lease_token

    row = db.get(OutboxTask, task.id)
    row.locked_at = utcnow() - timedelta(hours=1)
    db.commit()
    second = claim_task()
    assert second.lease_token != first.lease_token

    assert complete_task(second.id, second.lease_token) is True
    assert fail_task(first.id, first.lease_token, RuntimeError("旧 Worker 晚到")) is None
    db.expire_all()
    assert db.get(OutboxTask, task.id).status == OutboxStatus.done


def test_error_persistence_redacts_access_token(db):
    task = OutboxTask(task_type="sync", dedupe_key="redact:1", payload={})
    db.add(task)
    db.commit()
    claimed = claim_task()

    status = fail_task(
        claimed.id,
        claimed.lease_token,
        RuntimeError("POST https://example.invalid?access_token=super-secret&x=1"),
    )

    assert status == OutboxStatus.pending
    db.expire_all()
    error = db.get(OutboxTask, task.id).last_error
    assert "super-secret" not in error
    assert "access_token=[REDACTED]" in error


@pytest.mark.anyio
async def test_wecom_http_error_redacts_access_token_before_raising(monkeypatch):
    client = WeComClient()

    async def access_token():
        return "super-secret"

    request = httpx.Request(
        "POST", "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg?access_token=super-secret"
    )
    response = httpx.Response(500, request=request)

    class FailingHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.HTTPStatusError(
                f"request failed: {request.url}", request=request, response=response
            )

    monkeypatch.setattr(client, "access_token", access_token)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FailingHttpClient())

    with pytest.raises(RuntimeError) as captured:
        await client.call("/cgi-bin/kf/send_msg", {})

    assert "super-secret" not in str(captured.value)
    assert "access_token=[REDACTED]" in str(captured.value)
