from unittest.mock import Mock, patch

from redis.exceptions import RedisError

from wecom_ai_gateway.config import settings
from wecom_ai_gateway.models import (
    Conversation,
    Message,
    MessageStatus,
    OutboxStatus,
    OutboxTask,
    User,
)
from wecom_ai_gateway.tasks import (
    add_message_task,
    claim_task,
    complete_task,
    create_sync_task,
    distributed_lock,
    fail_task,
    reconcile_message_tasks,
    replay_task,
)


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


def test_message_task_is_deduplicated_and_claimed_once(db):
    message = make_message(db)
    first = add_message_task(db, message.id)
    second = add_message_task(db, message.id)
    db.commit()
    assert first.id == second.id
    claimed = claim_task()
    assert claimed.id == first.id
    assert claimed.status == OutboxStatus.processing
    assert claim_task() is None
    complete_task(claimed.id)
    db.expire_all()
    assert db.get(OutboxTask, first.id).status == OutboxStatus.done


def test_failed_task_retries_then_becomes_dead(db):
    message = make_message(db, "task-dead")
    task = add_message_task(db, message.id)
    db.commit()
    with (
        patch.object(settings, "task_max_attempts", 2),
        patch.object(settings, "task_retry_base_seconds", 1),
    ):
        status = fail_task(task.id, RuntimeError("temporary"))
        assert status == OutboxStatus.pending
        db.expire_all()
        retried = db.get(OutboxTask, task.id)
        assert retried.attempts == 1
        assert retried.last_error == "temporary"
        status = fail_task(task.id, RuntimeError("permanent"))
        assert status == OutboxStatus.dead
    db.expire_all()
    assert db.get(OutboxTask, task.id).status == OutboxStatus.dead
    assert db.get(Message, message.id).status == MessageStatus.dead
    with patch("wecom_ai_gateway.tasks.notify_worker", return_value=True):
        assert replay_task(task.id)
    db.expire_all()
    assert db.get(OutboxTask, task.id).status == OutboxStatus.pending
    assert db.get(OutboxTask, task.id).attempts == 0
    assert db.get(Message, message.id).status == MessageStatus.failed


def test_reconcile_repairs_missing_message_task(db):
    message = make_message(db, "task-reconcile")
    db.commit()
    with patch("wecom_ai_gateway.tasks.notify_worker", return_value=False):
        assert reconcile_message_tasks() == 1
        assert reconcile_message_tasks() == 0
    task = db.query(OutboxTask).filter_by(dedupe_key=f"message:{message.id}").one()
    assert task.status == OutboxStatus.pending


def test_distributed_lock_degrades_when_redis_is_unavailable():
    client = Mock()
    client.set.side_effect = RedisError("offline")
    with (
        patch("wecom_ai_gateway.tasks.redis_client", return_value=client),
        distributed_lock("test", 30) as acquired,
    ):
        assert acquired is True
    client.eval.assert_not_called()


def test_sync_callback_during_processing_requests_one_rerun(db):
    with patch("wecom_ai_gateway.tasks.notify_worker", return_value=True):
        task_id = create_sync_task("token-a", "account-a")
        task = claim_task()
        assert task.id == task_id
        create_sync_task("token-b", "account-a")
    db.expire_all()
    task = db.get(OutboxTask, task_id)
    assert task.rerun_requested is True
    assert task.payload["token"] == "token-b"
    complete_task(task_id)
    db.expire_all()
    task = db.get(OutboxTask, task_id)
    assert task.status == OutboxStatus.pending
    assert task.rerun_requested is False
