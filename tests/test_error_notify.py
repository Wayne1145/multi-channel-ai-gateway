"""模型全部失败时的用户错误通知测试。

当消息任务重试耗尽进入死信时，应通过新 Outbox 任务向用户投递一条精简错误
通知（不再调用 AI），内容与是否显示具体错误由管理员运行时设置控制。
"""
from unittest.mock import AsyncMock, patch

import pytest

from wecom_ai_gateway.config import settings
from wecom_ai_gateway.db import SessionLocal
from wecom_ai_gateway.models import (
    ChannelIdentity,
    Conversation,
    Message,
    MessageStatus,
    OutboxStatus,
    OutboxTask,
    User,
    UserSettings,
)
from wecom_ai_gateway.security import encrypt_secret
from wecom_ai_gateway.tasks import add_message_task, claim_task, fail_task


def make_message(db, msgid: str = "task-message") -> Message:
    user = User()
    db.add(user)
    db.flush()
    conversation = Conversation(user_id=user.id)
    db.add(conversation)
    db.flush()
    message = Message(
        user_id=user.id,
        conversation_id=conversation.id,
        channel="wecom_kf",
        external_message_id=msgid,
        direction="inbound",
        message_type="text",
        content="你好",
        status=MessageStatus.queued,
    )
    db.add(message)
    db.flush()
    return message


def test_dead_message_task_enqueues_error_notice(db):
    """消息任务进入死信时自动创建 notify 任务，供 worker 投递错误通知。"""
    message = make_message(db, "task-dead-notify")
    task = add_message_task(db, message.id)
    db.commit()
    claimed = claim_task()
    with (
        patch.object(settings, "task_max_attempts", 2),
        patch.object(settings, "task_retry_base_seconds", 1),
        patch("wecom_ai_gateway.tasks.notify_worker", return_value=True),
    ):
        # 第一次失败：进入 pending
        fail_task(task.id, claimed.lease_token, RuntimeError("temporary"))
        db.expire_all()
        retried = db.get(OutboxTask, task.id)
        retried.available_at = retried.available_at.replace(year=2000)
        db.commit()
        claimed = claim_task()
        # 第二次失败：进入 dead → 触发错误通知任务
        fail_task(task.id, claimed.lease_token, RuntimeError("模型服务不可用"))
    db.expire_all()
    assert db.get(OutboxTask, task.id).status == OutboxStatus.dead
    notify = (
        db.query(OutboxTask)
        .filter(OutboxTask.task_type == "notify")
        .all()
    )
    notify = next((n for n in notify if n.payload.get("message_id") == message.id), None)
    assert notify is not None
    assert notify.status == OutboxStatus.pending
    assert notify.payload["message_id"] == message.id


def test_dead_notify_task_does_not_create_another_notify(db):
    """错误通知任务自身失败不应再创建新的 notify，避免无限循环。"""
    message = make_message(db, "task-dead-notify-loop")
    task = add_message_task(db, message.id)
    db.commit()
    claimed = claim_task()
    with (
        patch.object(settings, "task_max_attempts", 2),
        patch.object(settings, "task_retry_base_seconds", 1),
        patch("wecom_ai_gateway.tasks.notify_worker", return_value=True),
    ):
        fail_task(task.id, claimed.lease_token, RuntimeError("temporary"))
        db.expire_all()
        retried = db.get(OutboxTask, task.id)
        retried.available_at = retried.available_at.replace(year=2000)
        db.commit()
        claimed = claim_task()
        fail_task(task.id, claimed.lease_token, RuntimeError("模型服务不可用"))
    db.expire_all()
    notify = (
        db.query(OutboxTask)
        .filter(OutboxTask.task_type == "notify")
        .all()
    )
    notify = next((n for n in notify if n.payload.get("message_id") == message.id), None)
    assert notify is not None
    # 再失败 notify 自身一次，不产生新 notify
    nclaimed = claim_task()
    assert nclaimed is not None and nclaimed.task_type == "notify"
    with (
        patch.object(settings, "task_max_attempts", 2),
        patch.object(settings, "task_retry_base_seconds", 1),
    ):
        fail_task(nclaimed.id, nclaimed.lease_token, RuntimeError("再失败"))
    count = (
        db.query(OutboxTask)
        .filter(OutboxTask.task_type == "notify")
        .all()
    )
    count = sum(1 for n in count if n.payload.get("message_id") == message.id)
    assert count == 1


@pytest.mark.anyio
async def test_notify_model_error_sends_text_with_detail(db, monkeypatch):
    """错误通知默认显示精简错误详情，通过企微 send_text 投递。"""
    from wecom_ai_gateway import services as svc
    from wecom_ai_gateway.runtime_settings import update_settings

    user = User()
    db.add(user)
    db.flush()
    conversation = Conversation(user_id=user.id)
    db.add(conversation)
    db.flush()
    identity = ChannelIdentity(
        user_id=user.id,
        channel="wecom_kf",
        account_id="wk-test",
        external_id_hash="a" * 64,
        external_id_encrypted=encrypt_secret("external-user"),
    )
    row = Message(
        user_id=user.id,
        conversation_id=conversation.id,
        channel="wecom_kf",
        external_message_id="notify-msg-1",
        direction="inbound",
        message_type="text",
        content="你好",
        status=MessageStatus.dead,
        error="模型服务不可用（上游 503）",
        metadata_json={"open_kfid": "wk-test"},
    )
    db.add_all([identity, row])
    db.commit()
    update_settings(db, {"model_error_message": "[error] 后端服务出现错误，请联系管理员。"})
    update_settings(db, {"model_error_show_detail": True})
    db.commit()

    send_text = AsyncMock(return_value="err-msg-1")
    monkeypatch.setattr(svc.client, "send_text", send_text)

    result = await svc.notify_model_error(db, row.id)
    assert result is True
    send_text.assert_awaited_once()
    text = send_text.await_args.args[2]
    assert text.startswith("[error]")
    assert "请联系管理员" in text
    assert "模型服务不可用" in text


@pytest.mark.anyio
async def test_notify_model_error_hides_detail_when_disabled(db, monkeypatch):
    """管理员关闭显示详情时，错误通知不带具体错误信息。"""
    from wecom_ai_gateway import services as svc
    from wecom_ai_gateway.runtime_settings import update_settings

    user = User()
    db.add(user)
    db.flush()
    conversation = Conversation(user_id=user.id)
    db.add(conversation)
    db.flush()
    identity = ChannelIdentity(
        user_id=user.id,
        channel="wecom_kf",
        account_id="wk-test",
        external_id_hash="b" * 64,
        external_id_encrypted=encrypt_secret("external-user"),
    )
    row = Message(
        user_id=user.id,
        conversation_id=conversation.id,
        channel="wecom_kf",
        external_message_id="notify-msg-2",
        direction="inbound",
        message_type="text",
        content="你好",
        status=MessageStatus.dead,
        error="模型服务不可用（上游 503）",
        metadata_json={"open_kfid": "wk-test"},
    )
    db.add_all([identity, row])
    db.commit()
    update_settings(db, {"model_error_message": "[error] 后端服务出现错误，请联系管理员。"})
    update_settings(db, {"model_error_show_detail": False})
    db.commit()

    send_text = AsyncMock(return_value="err-msg-2")
    monkeypatch.setattr(svc.client, "send_text", send_text)

    result = await svc.notify_model_error(db, row.id)
    assert result is True
    text = send_text.await_args.args[2]
    assert text.startswith("[error]")
    assert "请联系管理员" in text
    assert "模型服务不可用" not in text
    assert "503" not in text

@pytest.mark.anyio
async def test_error_notice_not_injected_into_conversation_history(db, monkeypatch):
    """error_notice 系统通知不得进入模型 history，避免模型复述 [error] 文案。

    回归：死信通知（error_notice=True）被当作 assistant 消息喂给模型后，
    SenseNova 误以为"上一条回复"是 [error] 文案并原样复述，用户收到
    "[error] 后端服务出现错误，请联系管理员。" 作为正常回复。
    """
    from wecom_ai_gateway import services as svc

    user = User()
    db.add(user)
    db.flush()
    us = UserSettings(user_id=user.id)
    db.add(us)
    conversation = Conversation(user_id=user.id)
    db.add(conversation)
    db.flush()
    # 正常用户消息
    row = Message(
        user_id=user.id,
        conversation_id=conversation.id,
        channel="wecom_kf",
        external_message_id="hist-msg-1",
        direction="inbound",
        message_type="text",
        content="再搜一次",
        status=MessageStatus.processing,
    )
    # 之前的死信通知（error_notice）
    notice = Message(
        conversation_id=conversation.id,
        user_id=user.id,
        channel="wecom_kf",
        external_message_id="hist-notice-1",
        direction="outbound",
        message_type="text",
        content="[error] 后端服务出现错误，请联系管理员。",
        status=MessageStatus.sent,
        metadata_json={"error_notice": True},
    )
    # 之前的正常回复
    normal = Message(
        conversation_id=conversation.id,
        user_id=user.id,
        channel="wecom_kf",
        external_message_id="hist-normal-1",
        direction="outbound",
        message_type="text",
        content="之前搜到的结果",
        status=MessageStatus.sent,
    )
    db.add_all([row, notice, normal])
    db.commit()
    row_id = row.id
    db.close()

    captured = []

    async def fake_complete_with_routing(db_arg, us_arg, prompts, **kw):
        captured.extend(prompts)
        from wecom_ai_gateway.model_routing import RoutedCompletion

        return RoutedCompletion(
            content="ok", provider_name="fake", provider_key="fake",
            model="fake", prompt_tokens=1, completion_tokens=1, tool_calls=[],
        )

    mr_module = __import__("sys").modules["wecom_ai_gateway.model_routing"]
    monkeypatch.setattr(mr_module, "complete_with_routing", fake_complete_with_routing)
    monkeypatch.setattr(
        mr_module,
        "active_routes",
        lambda db_arg, gid=None: [{"provider_id": "p1", "model": "m1", "priority": 10, "enabled": True}],
    )

    db = SessionLocal()
    row = db.get(Message, row_id)
    conv = db.get(Conversation, row.conversation_id)
    us = db.get(UserSettings, row.user_id)
    await svc._complete_ai(db, row, conv, us)
    db.close()

    history_text = "\n".join(
        str(p.get("content") or "") for p in captured if p.get("role") != "system"
    )
    assert "之前搜到的结果" in history_text, "正常回复应保留在历史中"
    assert "[error]" not in history_text, "error_notice 通知不得注入历史"
