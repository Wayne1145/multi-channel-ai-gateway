import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .config import settings
from .db import session_scope
from .models import (
    CharacterCard,
    CommandPolicy,
    Message,
    MessageStatus,
    OutboxStatus,
    OutboxTask,
    PlatformConfig,
    UsageRecord,
    User,
    UserSettings,
)
from .policy import resolve_user_mode
from .queueing import enqueue_sync
from .security import verify_admin_token
from .tasks import replay_task
from .wecom import decrypt, parse_callback, verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = FastAPI(
    title=settings.app_name,
    version="0.3.0",
    docs_url="/api/docs" if settings.app_env != "production" else None,
)
web = Path(__file__).resolve().parents[2] / "web"
app.mount("/static", StaticFiles(directory=web / "static"), name="static")


def admin(x_admin_token: str | None = Header(None)):
    if not verify_admin_token(x_admin_token):
        raise HTTPException(401, "invalid admin token")


def db_dep():
    yield from session_scope()


@app.get("/health")
def health(db: Session = Depends(db_dep)):
    db.execute(select(1))
    return {"ok": True, "service": "wecom-ai-gateway", "version": "0.3.0"}


@app.get(settings.wecom_callback_path)
def verify_callback(msg_signature: str, timestamp: str, nonce: str, echostr: str):
    if not verify_signature(msg_signature, timestamp, nonce, echostr):
        raise HTTPException(403, "signature mismatch")
    return Response(decrypt(echostr), media_type="text/plain")


@app.post(settings.wecom_callback_path)
async def callback(request: Request, msg_signature: str, timestamp: str, nonce: str):
    event = parse_callback(await request.body(), msg_signature, timestamp, nonce)
    if event.event == "kf_msg_or_event" and event.token:
        enqueue_sync(event.token, event.open_kfid)
    return Response("success", media_type="text/plain")


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return (web / "index.html").read_text(encoding="utf-8")


@app.get("/api/admin/stats", dependencies=[Depends(admin)])
def stats(db: Session = Depends(db_dep)):
    return {
        "users": db.scalar(select(func.count()).select_from(User)),
        "messages": db.scalar(select(func.count()).select_from(Message)),
        "failed": db.scalar(
            select(func.count()).select_from(Message).where(Message.status == MessageStatus.failed)
        ),
        "tokens": db.scalar(
            select(func.coalesce(func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens), 0))
        ),
        "mode": settings.platform_mode,
        "single_user_mode": settings.single_user_mode,
    }


@app.get("/api/admin/usage/trend", dependencies=[Depends(admin)])
def usage_trend(db: Session = Depends(db_dep), days: int = 7):
    days = max(1, min(days, 90))
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    rows = db.execute(
        select(
            func.date(UsageRecord.created_at).label("day"),
            func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens).label("tokens"),
        )
        .where(UsageRecord.created_at >= start)
        .group_by(func.date(UsageRecord.created_at))
        .order_by(func.date(UsageRecord.created_at))
    ).all()
    by_day = {str(r.day): int(r.tokens or 0) for r in rows}
    return [
        {
            "date": (start + timedelta(days=i)).date().isoformat(),
            "tokens": int(by_day.get((start + timedelta(days=i)).date().isoformat(), 0)),
        }
        for i in range(days)
    ]


@app.get("/api/admin/users", dependencies=[Depends(admin)])
def users(db: Session = Depends(db_dep), limit: int = 50):
    rows = db.execute(
        select(User, UserSettings)
        .outerjoin(UserSettings)
        .order_by(User.created_at.desc())
        .limit(min(limit, 200))
    ).all()
    return [
        {
            "id": u.id,
            "display_name": u.display_name,
            "blocked": u.is_blocked,
            "created_at": u.created_at,
            "model": s.model if s else None,
        }
        for u, s in rows
    ]


@app.post("/api/admin/users/{user_id}/block", dependencies=[Depends(admin)])
def block_user(user_id: str, blocked: bool = True, db: Session = Depends(db_dep)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    u.is_blocked = blocked
    db.commit()
    return {"ok": True, "blocked": u.is_blocked}


@app.get("/api/admin/messages", dependencies=[Depends(admin)])
def messages(db: Session = Depends(db_dep), limit: int = 100):
    rows = db.scalars(select(Message).order_by(Message.created_at.desc()).limit(min(limit, 500)))
    return [
        {
            "id": m.id,
            "user_id": m.user_id,
            "direction": m.direction,
            "type": m.message_type,
            "status": m.status,
            "content": m.content,
            "created_at": m.created_at,
            "error": m.error,
        }
        for m in rows
    ]


@app.get("/api/admin/tasks/dead", dependencies=[Depends(admin)])
def dead_tasks(db: Session = Depends(db_dep), limit: int = 100):
    rows = db.scalars(
        select(OutboxTask)
        .where(OutboxTask.status == OutboxStatus.dead)
        .order_by(OutboxTask.updated_at.desc())
        .limit(min(limit, 500))
    )
    return [
        {
            "id": task.id,
            "type": task.task_type,
            "attempts": task.attempts,
            "payload": task.payload,
            "error": task.last_error,
            "updated_at": task.updated_at,
        }
        for task in rows
    ]


@app.post("/api/admin/tasks/{task_id}/replay", dependencies=[Depends(admin)])
def replay_dead_task(task_id: str):
    if not replay_task(task_id):
        raise HTTPException(409, "task is missing or not dead")
    return {"ok": True, "task_id": task_id}


class ModeIn(BaseModel):
    mode: str | None = None  # self_service | managed | null(清除用户覆盖)


class PolicyIn(BaseModel):
    command: str
    channel: str | None = None  # None=全部渠道
    allowed: bool = True
    silent_block: bool = False
    blocked_strategy: str = "redirect_to_ai"


@app.get("/api/admin/mode", dependencies=[Depends(admin)])
def get_platform_mode(db: Session = Depends(db_dep)):
    row = db.get(PlatformConfig, "mode")
    return {
        "platform_mode": settings.platform_mode,
        "configured_mode": (row.value or {}).get("mode") if row else None,
        "single_user_mode": settings.single_user_mode,
    }


@app.post("/api/admin/mode", dependencies=[Depends(admin)])
def set_platform_mode(body: ModeIn, db: Session = Depends(db_dep)):
    if body.mode not in {None, "self_service", "managed"}:
        raise HTTPException(400, "mode must be self_service|managed|null")
    if body.mode is None:
        existing = db.get(PlatformConfig, "mode")
        if existing:
            db.delete(existing)
    else:
        row = db.get(PlatformConfig, "mode")
        if row:
            row.value = {"mode": body.mode}
        else:
            db.add(PlatformConfig(key="mode", value={"mode": body.mode}))
    db.commit()
    return {"ok": True, "mode": body.mode}


@app.get("/api/admin/users/{user_id}/mode", dependencies=[Depends(admin)])
def get_user_mode(user_id: str, db: Session = Depends(db_dep)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    return {"user_id": user_id, "mode": u.mode, "effective_mode": resolve_user_mode(db, u)}


@app.post("/api/admin/users/{user_id}/mode", dependencies=[Depends(admin)])
def set_user_mode(user_id: str, body: ModeIn, db: Session = Depends(db_dep)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    if body.mode not in {None, "self_service", "managed"}:
        raise HTTPException(400, "mode must be self_service|managed|null")
    u.mode = body.mode
    db.commit()
    return {"ok": True, "user_id": user_id, "mode": u.mode}


@app.get("/api/admin/users/{user_id}/cards", dependencies=[Depends(admin)])
def user_cards(user_id: str, db: Session = Depends(db_dep)):
    if not db.get(User, user_id):
        raise HTTPException(404, "user not found")
    rows = db.scalars(
        select(CharacterCard).where(CharacterCard.user_id == user_id).order_by(CharacterCard.created_at)
    )
    # 只返回元数据：卡内容入库即密文，管理端不提供解密出口
    return [
        {
            "id": c.id,
            "name": c.name,
            "format": c.format,
            "active": c.active,
            "created_at": c.created_at,
            "updated_at": c.updated_at,
        }
        for c in rows
    ]


@app.get("/api/admin/users/{user_id}/policies", dependencies=[Depends(admin)])
def user_policies(user_id: str, db: Session = Depends(db_dep)):
    rows = db.scalars(
        select(CommandPolicy)
        .where(or_(CommandPolicy.user_id == user_id, CommandPolicy.user_id.is_(None)))
        .order_by(CommandPolicy.command, CommandPolicy.user_id, CommandPolicy.channel)
    )
    return [
        {
            "id": p.id,
            "user_id": p.user_id,
            "channel": p.channel,
            "command": p.command,
            "allowed": p.allowed,
            "silent_block": p.silent_block,
            "blocked_strategy": p.blocked_strategy,
        }
        for p in rows
    ]


def _upsert_policy(db: Session, body: PolicyIn, user_id: str | None) -> CommandPolicy:
    command = body.command.lstrip("/").lower()
    if not command:
        raise HTTPException(400, "command is required")
    row = db.scalar(
        select(CommandPolicy).where(
            CommandPolicy.user_id == user_id,
            CommandPolicy.channel == body.channel,
            CommandPolicy.command == command,
        )
    )
    if row:
        row.allowed = body.allowed
        row.silent_block = body.silent_block
        row.blocked_strategy = body.blocked_strategy
    else:
        row = CommandPolicy(
            user_id=user_id,
            channel=body.channel,
            command=command,
            allowed=body.allowed,
            silent_block=body.silent_block,
            blocked_strategy=body.blocked_strategy,
        )
        db.add(row)
    db.flush()
    return row


@app.post("/api/admin/users/{user_id}/policies", dependencies=[Depends(admin)])
def set_user_policy(user_id: str, body: PolicyIn, db: Session = Depends(db_dep)):
    if not db.get(User, user_id):
        raise HTTPException(404, "user not found")
    row = _upsert_policy(db, body, user_id)
    db.commit()
    return {"ok": True, "policy_id": row.id}


@app.post("/api/admin/policies", dependencies=[Depends(admin)])
def set_platform_policy(body: PolicyIn, db: Session = Depends(db_dep)):
    row = _upsert_policy(db, body, None)
    db.commit()
    return {"ok": True, "policy_id": row.id}


@app.delete("/api/admin/policies/{policy_id}", dependencies=[Depends(admin)])
def delete_policy(policy_id: str, db: Session = Depends(db_dep)):
    row = db.get(CommandPolicy, policy_id)
    if not row:
        raise HTTPException(404, "policy not found")
    db.delete(row)
    db.commit()
    return {"ok": True, "deleted": policy_id}
