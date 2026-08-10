import logging
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from redis.exceptions import RedisError
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from .config import settings
from .db import SessionLocal
from .models import Message, MessageStatus, OutboxStatus, OutboxTask
from .queueing import notify_worker, redis_client
from .redaction import redact_error
from .runtime_settings import get_runtime_value

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


def sync_dedupe_key(token: str, open_kfid: str) -> str:
    # 每个客服账号只保留一个同步任务；新回调会更新 Token 并重新激活已完成任务。
    return f"sync:{open_kfid}"


def add_task(db, task_type: str, dedupe_key: str, payload: dict) -> OutboxTask:
    existing = db.scalar(select(OutboxTask).where(OutboxTask.dedupe_key == dedupe_key))
    if existing:
        return existing
    task = OutboxTask(task_type=task_type, dedupe_key=dedupe_key, payload=payload)
    db.add(task)
    db.flush()
    return task


def create_sync_task(token: str, open_kfid: str) -> str:
    db = SessionLocal()
    try:
        dedupe_key = sync_dedupe_key(token, open_kfid)
        task = db.scalar(select(OutboxTask).where(OutboxTask.dedupe_key == dedupe_key))
        if task:
            task.payload = {"token": token, "open_kfid": open_kfid}
            if task.status == OutboxStatus.processing:
                task.rerun_requested = True
            elif task.status in {OutboxStatus.done, OutboxStatus.dead}:
                task.status = OutboxStatus.pending
                task.attempts = 0
                task.available_at = utcnow()
                task.locked_at = None
                task.lease_token = None
                task.last_error = None
                task.rerun_requested = False
        else:
            task = add_task(
                db,
                "sync",
                dedupe_key,
                {"token": token, "open_kfid": open_kfid},
            )
        db.commit()
        task_id = task.id
    except IntegrityError:
        db.rollback()
        task_id = db.scalar(
            select(OutboxTask.id).where(
                OutboxTask.dedupe_key == sync_dedupe_key(token, open_kfid)
            )
        )
    finally:
        db.close()
    notify_worker()
    return task_id


def add_message_task(db, message_id: str) -> OutboxTask:
    return add_task(db, "message", f"message:{message_id}", {"message_id": message_id})


def claim_task() -> OutboxTask | None:
    db = SessionLocal()
    now = utcnow()
    stale = now - timedelta(seconds=int(get_runtime_value(db, "task_lock_timeout_seconds")))
    try:
        task_id = db.scalar(
            select(OutboxTask.id)
            .where(
                or_(
                    (OutboxTask.status == OutboxStatus.pending)
                    & (OutboxTask.available_at <= now),
                    (OutboxTask.status == OutboxStatus.processing)
                    & (OutboxTask.locked_at < stale),
                )
            )
            .order_by(OutboxTask.available_at, OutboxTask.created_at)
            .limit(1)
        )
        if not task_id:
            return None
        claimed = db.execute(
            update(OutboxTask)
            .where(
                OutboxTask.id == task_id,
                or_(
                    (OutboxTask.status == OutboxStatus.pending)
                    & (OutboxTask.available_at <= now),
                    (OutboxTask.status == OutboxStatus.processing)
                    & (OutboxTask.locked_at < stale),
                ),
            )
            .values(
                status=OutboxStatus.processing,
                locked_at=now,
                lease_token=str(uuid.uuid4()),
            )
        )
        if claimed.rowcount != 1:
            db.rollback()
            return None
        db.commit()
        return db.get(OutboxTask, task_id)
    finally:
        db.close()


def renew_task_lease(task_id: str, lease_token: str) -> bool:
    """续租当前 Worker 的数据库租约；租约已被接管时返回 False。"""
    db = SessionLocal()
    try:
        result = db.execute(
            update(OutboxTask)
            .where(
                OutboxTask.id == task_id,
                OutboxTask.status == OutboxStatus.processing,
                OutboxTask.lease_token == lease_token,
            )
            .values(locked_at=utcnow())
        )
        db.commit()
        return result.rowcount == 1
    finally:
        db.close()


def complete_task(task_id: str, lease_token: str | None = None) -> bool:
    db = SessionLocal()
    try:
        conditions = [
            OutboxTask.id == task_id,
            OutboxTask.status == OutboxStatus.processing,
        ]
        if lease_token is not None:
            conditions.append(OutboxTask.lease_token == lease_token)
        task = db.scalar(select(OutboxTask).where(*conditions))
        if not task:
            return False
        if task.task_type == "sync" and task.rerun_requested:
            task.status = OutboxStatus.pending
            task.available_at = utcnow()
            task.rerun_requested = False
        else:
            task.status = OutboxStatus.done
        task.locked_at = None
        task.lease_token = None
        task.last_error = None
        db.commit()
        return True
    finally:
        db.close()


def fail_task(
    task_id: str, lease_token: str | Exception | None, error: Exception | None = None
) -> OutboxStatus | None:
    """按租约记录失败；旧 Worker 的迟到结果不会覆盖新租约。"""
    if error is None and isinstance(lease_token, Exception):
        error = lease_token
        lease_token = None
    db = SessionLocal()
    try:
        conditions = [
            OutboxTask.id == task_id,
            OutboxTask.status == OutboxStatus.processing,
        ]
        if lease_token is not None:
            conditions.append(OutboxTask.lease_token == lease_token)
        task = db.scalar(select(OutboxTask).where(*conditions))
        if not task:
            return None
        task.attempts += 1
        task.last_error = redact_error(error or "unknown error")
        task.locked_at = None
        task.lease_token = None
        if task.attempts >= int(get_runtime_value(db, "task_max_attempts")):
            task.status = OutboxStatus.dead
            try:
                from .alert import send_alert

                send_alert(
                    f"[{settings.app_name}] 任务进入死信",
                    f"task={task.id}\ntype={task.task_type}\nattempts={task.attempts}\n"
                    f"error={task.last_error or ''}",
                )
            except Exception:
                log.exception("死信告警失败 task=%s", task.id)
            if task.task_type == "message":
                message = db.get(Message, task.payload.get("message_id"))
                if message:
                    message.status = MessageStatus.dead
                    message.error = task.last_error
        else:
            delay = min(
                int(get_runtime_value(db, "task_retry_base_seconds")) * (2 ** (task.attempts - 1)),
                int(get_runtime_value(db, "task_retry_max_seconds")),
            )
            task.status = OutboxStatus.pending
            task.available_at = utcnow() + timedelta(seconds=delay)
        db.commit()
        return task.status
    finally:
        db.close()


def replay_task(task_id: str) -> bool:
    db = SessionLocal()
    try:
        task = db.get(OutboxTask, task_id)
        if not task or task.status != OutboxStatus.dead:
            return False
        task.status = OutboxStatus.pending
        task.attempts = 0
        task.available_at = utcnow()
        task.locked_at = None
        task.lease_token = None
        task.last_error = None
        if task.task_type == "message":
            message = db.get(Message, task.payload.get("message_id"))
            if message and message.status == MessageStatus.dead:
                message.status = MessageStatus.failed
                message.error = None
        db.commit()
    finally:
        db.close()
    notify_worker()
    return True


def reconcile_message_tasks(limit: int = 500) -> int:
    db = SessionLocal()
    created = 0
    try:
        message_ids = list(
            db.scalars(
                select(Message.id)
                .outerjoin(OutboxTask, OutboxTask.dedupe_key == ("message:" + Message.id))
                .where(
                    Message.status.in_([MessageStatus.queued, MessageStatus.failed]),
                    OutboxTask.id.is_(None),
                )
                .limit(limit)
            )
        )
        for message_id in message_ids:
            add_message_task(db, message_id)
            created += 1
        db.commit()
    finally:
        db.close()
    if created:
        notify_worker()
    return created


@contextmanager
def distributed_lock(name: str, ttl_seconds: int):
    client = redis_client()
    key = f"wecom-ai:lock:{name}"
    token = str(uuid.uuid4())
    acquired = False
    redis_available = True
    try:
        acquired = bool(client.set(key, token, nx=True, ex=ttl_seconds))
    except RedisError:
        # Outbox 的原子领取仍能保证同一任务不会被两个 Worker 同时执行。
        # Redis 故障时降级继续，避免基础设施故障消耗业务重试次数。
        redis_available = False
        acquired = True
        log.warning("Redis 锁不可用，任务依靠数据库原子领取继续执行：%s", name)
    try:
        yield acquired
    finally:
        if acquired and redis_available:
            try:
                client.eval(
                    "if redis.call('get',KEYS[1]) == ARGV[1] then "
                    "return redis.call('del',KEYS[1]) else return 0 end",
                    1,
                    key,
                    token,
                )
            except RedisError:
                log.warning("释放 Redis 锁失败，将等待 TTL 自动过期：%s", name)
