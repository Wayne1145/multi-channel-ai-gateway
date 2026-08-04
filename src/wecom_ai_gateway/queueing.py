import logging

from redis import Redis
from redis.exceptions import RedisError

from .config import settings

log = logging.getLogger(__name__)
WAKE_QUEUE = "wecom-ai:wake"


def redis_client():
    return Redis.from_url(settings.redis_url, decode_responses=True)


def notify_worker() -> bool:
    """通知 Worker 扫描数据库；失败不丢任务，因为任务已持久化在 Outbox。"""
    try:
        redis_client().rpush(WAKE_QUEUE, "1")
        return True
    except RedisError:
        log.exception("Redis 通知失败，Worker 将通过定时扫描补偿")
        return False


def enqueue_message(message_id):
    """兼容旧调用：消息任务由持久化 Outbox 创建。"""
    from .db import SessionLocal
    from .tasks import add_message_task

    db = SessionLocal()
    try:
        add_message_task(db, message_id)
        db.commit()
    finally:
        db.close()
    notify_worker()


def enqueue_sync(token, open_kfid):
    from .tasks import create_sync_task

    return create_sync_task(token, open_kfid)
