import json

from redis import Redis

from .config import settings


def redis_client():
    return Redis.from_url(settings.redis_url, decode_responses=True)


def enqueue_message(message_id):
    redis_client().rpush("wecom-ai:messages", message_id)


def enqueue_sync(token, open_kfid):
    redis_client().rpush("wecom-ai:sync", json.dumps({"token": token, "open_kfid": open_kfid}))
