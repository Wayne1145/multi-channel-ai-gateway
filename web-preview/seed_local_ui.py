"""临时本地 UI 验证：SQLite 建表 + 种子数据 + 启动 uvicorn。"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/admin-ui-test.db")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token-2026")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from wecom_ai_gateway.db import Base, SessionLocal, engine
from wecom_ai_gateway import models  # noqa: F401  确保全部模型注册
from wecom_ai_gateway.models import (
    Message, MessageDirection, MessageStatus, OutboxStatus, OutboxTask,
    UsageRecord, User, UserSettings,
)

Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)

db = SessionLocal()
now = datetime.now(timezone.utc)

def uid():
    return str(uuid.uuid4())

# 用户
users = []
for name, mode, blocked in [
    ("Wayne", "managed", False),
    ("月读访客", "self_service", False),
    ("测试账号", "managed", True),
    ("新用户 0731", None, False),
]:
    u = User(id=uid(), display_name=name, mode=mode, is_blocked=blocked, created_at=now - timedelta(days=30))
    db.add(u)
    db.add(UserSettings(user_id=u.id))
    users.append(u)
db.commit()

# 消息（近 7 天，混合方向/状态）
statuses = [MessageStatus.sent, MessageStatus.sent, MessageStatus.queued,
            MessageStatus.failed, MessageStatus.ignored, MessageStatus.sent, MessageStatus.dead]
for i in range(80):
    day = now - timedelta(hours=i * 3)
    u = users[i % len(users)]
    direction = MessageDirection.inbound if i % 3 != 0 else MessageDirection.outbound
    status = statuses[i % len(statuses)]
    content = f"示例消息 {i}: 这是一条{'入站' if direction == MessageDirection.inbound else '出站'}消息，用于本地界面预览。"
    db.add(Message(
        id=uid(), user_id=u.id, channel="wecom_kf",
        external_message_id=f"ext-{i}",
        direction=direction, message_type="text",
        content=content if status != MessageStatus.failed else None,
        status=status,
        metadata_json={},
        error=None if status != MessageStatus.failed else "模型尚未生成可发送的最终内容 (attempt 5/5)",
        created_at=day,
    ))

# 用量（近 7 天）
for d in range(7):
    day = (now - timedelta(days=6 - d)).replace(hour=12, minute=0, second=0, microsecond=0)
    base = 800 + d * 470 + (d % 3) * 220
    db.add(UsageRecord(
        id=uid(), user_id=users[0].id, provider="deepseek", model="deepseek-v4-flash",
        prompt_tokens=base, completion_tokens=base // 2,
        created_at=day,
    ))

# 死信任务
for i in range(2):
    db.add(OutboxTask(
        id=uid(), task_type="process_message", dedupe_key=f"dead-{i}",
        status=OutboxStatus.dead, attempts=5,
        last_error="上游供应商超时 (attempt 5/5)" if i else "模型尚未生成可发送的最终内容 (attempt 5/5)",
        payload={},
        created_at=now - timedelta(hours=6 + i * 10), updated_at=now - timedelta(hours=i),
    ))

db.commit()
db.close()
print("SEED_OK")
