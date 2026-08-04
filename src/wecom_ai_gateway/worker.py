import asyncio
import json
import logging

from .queueing import redis_client
from .services import process_message, sync_wecom_messages

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


async def main():
    r = redis_client()
    log.info("任务 Worker 已启动")
    while True:
        item = await asyncio.to_thread(r.blpop, ["wecom-ai:sync", "wecom-ai:messages"], 5)
        if not item:
            continue
        queue, payload = item
        try:
            if queue.endswith("sync"):
                d = json.loads(payload)
                await sync_wecom_messages(d["token"], d["open_kfid"])
            else:
                await process_message(payload)
        except Exception:
            log.exception("任务执行失败，稍后重新入队")
            await asyncio.sleep(2)
            r.rpush(queue, payload)


if __name__ == "__main__":
    asyncio.run(main())
