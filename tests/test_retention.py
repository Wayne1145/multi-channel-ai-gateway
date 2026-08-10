"""数据保留清理测试：按运行时保留天数删除旧消息/死信/审计日志，默认关闭。"""

from datetime import UTC, datetime, timedelta

from wecom_ai_gateway.models import AuditLog, Message, MessageStatus, OutboxStatus, OutboxTask
from wecom_ai_gateway.retention import cleanup_expired_data
from wecom_ai_gateway.runtime_settings import update_settings


def _old_message(db, days: int) -> Message:
    row = Message(
        channel="wecom_kf",
        channel_instance_id="kf-1",
        external_message_id=f"old-{days}",
        direction="inbound",
        message_type="text",
        content="旧消息",
        status=MessageStatus.sent,
    )
    db.add(row)
    db.flush()
    db.execute(
        Message.__table__.update()
        .where(Message.id == row.id)
        .values(created_at=datetime.now(UTC) - timedelta(days=days))
    )
    return row


def _old_task(db, days: int) -> OutboxTask:
    task = OutboxTask(task_type="message", dedupe_key=f"dead-{days}", payload={}, status=OutboxStatus.dead)
    db.add(task)
    db.flush()
    db.execute(
        OutboxTask.__table__.update()
        .where(OutboxTask.id == task.id)
        .values(created_at=datetime.now(UTC) - timedelta(days=days))
    )
    return task


def _old_audit(db, days: int) -> AuditLog:
    log = AuditLog(action="test", detail={})
    db.add(log)
    db.flush()
    db.execute(
        AuditLog.__table__.update()
        .where(AuditLog.id == log.id)
        .values(created_at=datetime.now(UTC) - timedelta(days=days))
    )
    return log


def test_retention_defaults_off(db):
    old_msg = _old_message(db, 999)
    old_task = _old_task(db, 999)
    old_log = _old_audit(db, 999)
    db.commit()

    removed = cleanup_expired_data(db)

    assert removed == {"messages": 0, "dead_tasks": 0, "audit_logs": 0}
    assert db.get(Message, old_msg.id) is not None
    assert db.get(OutboxTask, old_task.id) is not None
    assert db.get(AuditLog, old_log.id) is not None


def test_retention_deletes_only_expired_rows(db):
    update_settings(
        db,
        {"message_retention_days": 30, "dead_task_retention_days": 30, "audit_retention_days": 30},
    )
    expired_msg = _old_message(db, 60)
    fresh_msg = _old_message(db, 5)
    expired_task = _old_task(db, 60)
    fresh_task = _old_task(db, 5)
    expired_log = _old_audit(db, 60)
    fresh_log = _old_audit(db, 5)
    db.commit()

    removed = cleanup_expired_data(db)
    db.expire_all()

    assert removed == {"messages": 1, "dead_tasks": 1, "audit_logs": 1}
    assert db.get(Message, expired_msg.id) is None
    assert db.get(Message, fresh_msg.id) is not None
    assert db.get(OutboxTask, expired_task.id) is None
    assert db.get(OutboxTask, fresh_task.id) is not None
    assert db.get(AuditLog, expired_log.id) is None
    assert db.get(AuditLog, fresh_log.id) is not None


def test_retention_does_not_delete_active_tasks(db):
    update_settings(db, {"dead_task_retention_days": 7})
    pending = OutboxTask(task_type="sync", dedupe_key="sync-1", payload={}, status=OutboxStatus.pending)
    db.add(pending)
    db.flush()
    db.execute(
        OutboxTask.__table__.update()
        .where(OutboxTask.id == pending.id)
        .values(created_at=datetime.now(UTC) - timedelta(days=90))
    )
    db.commit()

    cleanup_expired_data(db)

    assert db.get(OutboxTask, pending.id) is not None
