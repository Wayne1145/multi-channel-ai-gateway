import asyncio
import logging

from .clawbot import register_clawbot_adapter
from .config import settings
from .media import cleanup_expired_media
from .queueing import WAKE_QUEUE, redis_client
from .redaction import redact_error
from .services import process_message, sync_wecom_messages
from .tasks import (
    claim_task,
    complete_task,
    distributed_lock,
    fail_task,
    reconcile_message_tasks,
    renew_task_lease,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


def register_worker_adapters() -> None:
    """注册 Worker 处理出站消息所需的渠道适配器。"""
    register_clawbot_adapter()


def migrate_legacy_knowledge() -> int:
    """启动时把升级前的明文知识条目原地迁入加密分块。"""
    from .db import SessionLocal
    from .knowledge import migrate_legacy_items

    db = SessionLocal()
    try:
        return migrate_legacy_items(db)
    finally:
        db.close()


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
    if task.task_type == "notify":
        # 模型故障错误通知：不调用 AI，直接投递死信提示；自身失败不会创建新 notify。
        from .db import SessionLocal
        from .services import notify_model_error

        db = SessionLocal()
        try:
            ok = await notify_model_error(db, task.payload["message_id"])
            if not ok:
                raise RuntimeError("模型故障错误通知投递失败")
        finally:
            db.close()
        return
    if task.task_type == "alert":
        from .alert import send_alert

        payload = task.payload or {}
        kind = payload.get("kind")
        name = str(payload.get("instance_name") or "未命名实例")[:120]
        label = "渠道已恢复" if kind == "channel_recovered" else "渠道连接异常"
        body = (
            f"instance={name}\n"
            f"channel={str(payload.get('channel') or 'wechat_clawbot')[:40]}\n"
            f"transition={str(payload.get('old_status') or '')[:20]}"
            f"->{str(payload.get('new_status') or '')[:20]}\n"
            f"error={str(payload.get('error') or '')[:120]}"
        )
        await asyncio.to_thread(send_alert, f"[{settings.app_name}] {label}：{name}", body)
        return
    raise ValueError(f"未知任务类型：{task.task_type}")


async def drain_available_tasks() -> int:
    processed = 0
    while task := claim_task():
        stop_heartbeat = asyncio.Event()

        async def heartbeat(stop_event=stop_heartbeat, task_id=task.id, lease_token=task.lease_token):
            interval = max(1, settings.task_lock_timeout_seconds // 3)
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                except TimeoutError:
                    if not await asyncio.to_thread(renew_task_lease, task_id, lease_token):
                        log.error("任务租约已丢失 id=%s", task_id)
                        return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            await execute_task(task)
        except Exception as exc:  # noqa: BLE001 - Worker 必须把业务异常持久化为可重试任务。
            status = fail_task(task.id, task.lease_token, exc)
            status_text = status.value if status is not None else "lease-lost"
            log.error(
                "任务失败 id=%s type=%s status=%s error=%s",
                task.id,
                task.task_type,
                status_text,
                redact_error(exc, 300),
            )
        else:
            complete_task(task.id, task.lease_token)
        finally:
            stop_heartbeat.set()
            await heartbeat_task
        processed += 1
    return processed


async def channel_reconcile_loop(interval_seconds: float = 30.0) -> None:
    """独立运行渠道状态对账，绝不阻塞 Outbox 消费主循环。"""
    from .channel_health import reconcile_all_channel_instances
    from .db import SessionLocal

    while True:
        db = SessionLocal()
        try:
            await reconcile_all_channel_instances(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("渠道实例状态对账失败")
        finally:
            db.close()
        await asyncio.sleep(interval_seconds)


async def main():
    register_worker_adapters()
    migrated = await asyncio.to_thread(migrate_legacy_knowledge)
    if migrated:
        log.info("已将 %s 条旧知识库明文迁移为加密分块", migrated)
    r = redis_client()
    log.info("任务 Worker 已启动（持久化 Outbox 模式）")
    asyncio.create_task(channel_reconcile_loop())
    last_reconcile = 0.0
    last_media_cleanup = 0.0
    last_retention_cleanup = 0.0
    while True:
        now = asyncio.get_running_loop().time()
        if now - last_reconcile >= 60:
            repaired = await asyncio.to_thread(reconcile_message_tasks)
            if repaired:
                log.warning("补偿创建了 %s 个消息任务", repaired)
            last_reconcile = now

        if now - last_media_cleanup >= 3600:
            from .db import SessionLocal

            db = SessionLocal()
            try:
                removed = cleanup_expired_media(db)
                db.commit()
                if removed:
                    log.info("媒体清理：删除 %s 条过期记录", removed)
            except Exception:
                log.exception("媒体过期清理失败")
            finally:
                db.close()
            last_media_cleanup = now
        if now - last_retention_cleanup >= 3600:
            from .db import SessionLocal
            from .retention import cleanup_expired_data

            db = SessionLocal()
            try:
                removed = cleanup_expired_data(db)
                if sum(removed.values()):
                    log.info("数据保留清理：%s", removed)
            except Exception:
                log.exception("数据保留清理失败")
            finally:
                db.close()
            last_retention_cleanup = now
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
