"""数据保留清理：按运行时保留天数删除过期消息、死信任务与审计日志。

默认全部为 0（不清理），由管理员在平台设置中显式开启。删除范围：
- 消息：仅删除超过保留天数的消息记录（含其会话保持不动）；
- 死信任务：仅删除 status=dead 且超期的任务；
- 审计日志：超过保留天数的审计记录。

调用方为 Worker 的每小时清理循环，与媒体 TTL 清理同节奏。
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import AuditLog, Message, OutboxStatus, OutboxTask
from .runtime_settings import get_runtime_value

log = logging.getLogger(__name__)


def cleanup_expired_data(db: Session, now: datetime | None = None) -> dict[str, int]:
    """执行数据保留清理，返回各分类删除条数。"""
    cutoff = now or datetime.now(UTC)
    removed = {"messages": 0, "dead_tasks": 0, "audit_logs": 0}

    message_days = int(get_runtime_value(db, "message_retention_days"))
    if message_days > 0:
        limit = cutoff - timedelta(days=message_days)
        ids = list(db.scalars(select(Message.id).where(Message.created_at < limit)))
        if ids:
            db.execute(delete(Message).where(Message.id.in_(ids)))
            removed["messages"] = len(ids)

    dead_days = int(get_runtime_value(db, "dead_task_retention_days"))
    if dead_days > 0:
        limit = cutoff - timedelta(days=dead_days)
        ids = list(
            db.scalars(
                select(OutboxTask.id).where(
                    OutboxTask.status == OutboxStatus.dead,
                    OutboxTask.created_at < limit,
                )
            )
        )
        if ids:
            db.execute(delete(OutboxTask).where(OutboxTask.id.in_(ids)))
            removed["dead_tasks"] = len(ids)

    audit_days = int(get_runtime_value(db, "audit_retention_days"))
    if audit_days > 0:
        limit = cutoff - timedelta(days=audit_days)
        ids = list(db.scalars(select(AuditLog.id).where(AuditLog.created_at < limit)))
        if ids:
            db.execute(delete(AuditLog).where(AuditLog.id.in_(ids)))
            removed["audit_logs"] = len(ids)

    if sum(removed.values()):
        db.commit()
        log.info("数据保留清理：%s", removed)
    return removed
