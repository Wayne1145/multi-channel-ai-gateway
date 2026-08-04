import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import session_scope
from .models import Message, MessageStatus, UsageRecord, User, UserSettings
from .queueing import enqueue_sync
from .security import verify_admin_token
from .wecom import decrypt, parse_callback, verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
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
    return {"ok": True, "service": "wecom-ai-gateway", "version": "0.1.0"}


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
    }


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
