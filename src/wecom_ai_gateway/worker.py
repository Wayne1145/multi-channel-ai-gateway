import asyncio
import logging

from .config import settings
from .queueing import WAKE_QUEUE, redis_client
from .services import process_message, sync_wecom_messages
from .tasks import (
    claim_task,
    complete_task,
    distributed_lock,
    fail_task,
    reconcile_message_tasks,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


async def execute_task(task) -> None:
    if task.task_type == "sync":
        open_kfid = task.payload.get("open_kfid", "")
        with distributed_lock(f"sync:{open_kfid}", settings.sync_lock_seconds) as acquired:
            if not acquired:
                raise RuntimeError(f"客服账号同步任务正在执行：{open_kfid}")
            await sync_wecom_messages(task.payload["token"], open_kfid)
        return
    if task.task_type == "message":
        message_id = task.payload["message_id"]
        with distributed_lock(f"message:{message_id}", settings.task_lock_timeout_seconds) as acquired:
            if not acquired:
                raise RuntimeError(f"消息任务正在执行：{message_id}")
            await process_message(message_id)
        return
    raise ValueError(f"未知任务类型：{task.task_type}")


async def drain_available_tasks() -> int:
    processed = 0
    while task := claim_task():
        try:
            await execute_task(task)
        except Exception as exc:
            status = fail_task(task.id, exc)
            log.exception("任务失败 id=%s type=%s status=%s", task.id, task.task_type, status.value)
        else:
            complete_task(task.id)
        processed += 1
    return processed


async def main():
    r = redis_client()
    log.info("任务 Worker 已启动（持久化 Outbox 模式）")
    last_reconcile = 0.0
    while True:
        now = asyncio.get_running_loop().time()
        if now - last_reconcile >= 60:
            repaired = await asyncio.to_thread(reconcile_message_tasks)
            if repaired:
                log.warning("补偿创建了 %s 个消息任务", repaired)
            last_reconcile = now
        processed = await drain_available_tasks()
        if processed:
            continue
        try:
            await asyncio.to_thread(r.blpop, WAKE_QUEUE, settings.worker_poll_seconds)
        except Exception:
            log.exception("Redis 等待失败，退回数据库轮询")
            await asyncio.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    asyncio.run(main())
