import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .cards import detect_format, encrypt_card_content, extract_card_from_png
from .channels import ChannelMessage, registry
from .clawbot import register_clawbot_adapter
from .config import settings
from .db import SessionLocal, session_scope
from .media import list_media_metadata
from .migration import migrate_user_mode
from .models import (
    ChannelInstance,
    CharacterCard,
    CommandPolicy,
    Conversation,
    MediaAsset,
    Memory,
    Message,
    MessageStatus,
    OutboxStatus,
    OutboxTask,
    PlatformConfig,
    Preset,
    UsageRecord,
    User,
    UserProvider,
    UserSettings,
)
from .policy import resolve_user_mode
from .queueing import enqueue_sync
from .redaction import redact_error
from .security import verify_admin_token
from .services import ingest_channel_message
from .tasks import replay_task
from .wecom import decrypt, parse_callback, verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)
app = FastAPI(
    title=settings.app_name,
    version="0.3.0",
    docs_url="/api/docs" if settings.app_env != "production" else None,
)
web = Path(__file__).resolve().parents[2] / "web"
app.mount("/static", StaticFiles(directory=web / "static"), name="static")
register_clawbot_adapter()


def admin(x_admin_token: str | None = Header(None)):
    if not verify_admin_token(x_admin_token):
        raise HTTPException(401, "invalid admin token")


def bridge_auth(authorization: str | None = Header(default=None)) -> None:
    """桥接服务只能以独立令牌写入消息，不能复用管理员令牌。"""
    token = settings.clawbot_bridge_token
    if not token:
        raise HTTPException(503, "channel bridge token is not configured")
    if authorization != f"Bearer {token}":
        raise HTTPException(401, "invalid channel bridge token")


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
        "mode": resolve_user_mode(db, None),
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
            "mode": u.mode,
            "effective_mode": resolve_user_mode(db, u),
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


@app.get("/api/admin/media", dependencies=[Depends(admin)])
def media_metadata(db: Session = Depends(db_dep), limit: int = 100):
    """媒体元数据审计：不含 storage_key/URL 等可能携带凭据的字段。"""
    return list_media_metadata(db, limit=limit)


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
    blocked_strategy: Literal["redirect_to_ai", "ignore"] = "redirect_to_ai"


class ChannelInstanceIn(BaseModel):
    """仅保存公开实例配置；登录态和会话凭据只能由桥接服务加密写入。"""

    channel: Literal["wechat_clawbot"]
    instance_name: str
    owner_user_id: str | None = None
    config: dict = {}


class ChannelMessageIn(BaseModel):
    """桥接服务投递的规范化入站消息；端点只面向受保护的内部网络。"""

    sender_id: str
    external_message_id: str
    message_type: str = "text"
    content: str | None = None
    media: list[dict] = []
    raw: dict = {}


def _channel_instance_view(instance: ChannelInstance) -> dict:
    """管理端只返回非敏感元数据，绝不返回 session_encrypted/login_state 原文。"""
    return {
        "id": instance.id,
        "channel": instance.channel,
        "instance_name": instance.instance_name,
        "owner_user_id": instance.owner_user_id,
        "status": instance.status,
        "config": instance.config,
        "created_at": instance.created_at,
        "updated_at": instance.updated_at,
    }


@app.get("/api/admin/channel-instances", dependencies=[Depends(admin)])
def channel_instances(db: Session = Depends(db_dep)):
    rows = db.scalars(select(ChannelInstance).order_by(ChannelInstance.created_at.desc()))
    return [_channel_instance_view(instance) for instance in rows]


@app.post("/api/admin/channel-instances", dependencies=[Depends(admin)])
def create_channel_instance(body: ChannelInstanceIn, db: Session = Depends(db_dep)):
    if not body.instance_name.strip():
        raise HTTPException(400, "instance_name is required")
    if body.owner_user_id and not db.get(User, body.owner_user_id):
        raise HTTPException(404, "owner user not found")
    try:
        registry.get(body.channel)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    instance = ChannelInstance(
        channel=body.channel,
        instance_name=body.instance_name.strip()[:120],
        owner_user_id=body.owner_user_id,
        config=body.config,
        login_state={},
        status="offline",
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return _channel_instance_view(instance)


async def _change_channel_instance_status(instance_id: str, action: Literal["start", "stop"]) -> dict:
    """桥接调用完成后才写状态，失败时保存可见但不含敏感信息的 error 状态。"""
    db = SessionLocal()
    try:
        instance = db.get(ChannelInstance, instance_id)
        if not instance:
            raise HTTPException(404, "channel instance not found")
        try:
            adapter = registry.get(instance.channel)
            if action == "start":
                instance.status = "logging_in"
                db.commit()
                await adapter.start_instance(instance.id)
                instance.status = "online"
            else:
                await adapter.stop_instance(instance.id)
                instance.status = "offline"
            db.commit()
        except Exception as exc:
            db.rollback()
            instance = db.get(ChannelInstance, instance_id)
            if instance:
                instance.status = "error"
                db.commit()
            log.error(
                "渠道实例操作失败 id=%s action=%s error=%s",
                instance_id,
                action,
                redact_error(exc, 300),
            )
            raise HTTPException(502, "channel bridge operation failed") from exc
        return _channel_instance_view(instance)
    finally:
        db.close()


@app.post("/api/admin/channel-instances/{instance_id}/start", dependencies=[Depends(admin)])
async def start_channel_instance(instance_id: str):
    return await _change_channel_instance_status(instance_id, "start")


@app.post("/api/admin/channel-instances/{instance_id}/stop", dependencies=[Depends(admin)])
async def stop_channel_instance(instance_id: str):
    return await _change_channel_instance_status(instance_id, "stop")


@app.post("/api/internal/channel-instances/{instance_id}/messages", dependencies=[Depends(bridge_auth)])
def receive_channel_message(instance_id: str, body: ChannelMessageIn, db: Session = Depends(db_dep)):
    """受保护的桥接入口：复用统一身份映射与消息 Outbox，不暴露给终端用户。"""
    instance = db.get(ChannelInstance, instance_id)
    if not instance:
        raise HTTPException(404, "channel instance not found")
    if instance.status != "online":
        raise HTTPException(409, "channel instance is not online")
    message = ingest_channel_message(
        db,
        ChannelMessage(
            channel=instance.channel,
            instance_id=instance.id,
            sender_id=body.sender_id,
            external_message_id=body.external_message_id,
            message_type=body.message_type,
            content=body.content,
            media=body.media,
            raw=body.raw,
        ),
    )
    db.commit()
    return {"ok": True, "accepted": message is not None, "message_id": message.id if message else None}


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
    summary = migrate_user_mode(db, u, body.mode)
    db.commit()
    return {"ok": True, "user_id": user_id, "mode": u.mode, "migration": summary}


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


class CardImportIn(BaseModel):
    """角色卡导入：content 直接给文本（SOUL.md 或 SillyTavern JSON）；
    png_base64 给 PNG 内嵌卡数据（v2/v3 自动提取）。"""

    name: str
    content: str | None = None
    png_base64: str | None = None


@app.post("/api/admin/users/{user_id}/cards/import", dependencies=[Depends(admin)])
def import_user_card(user_id: str, body: CardImportIn, db: Session = Depends(db_dep)):
    """管理员替用户导入角色卡（不读卡内容，仅落库加密）。"""
    if not db.get(User, user_id):
        raise HTTPException(404, "user not found")
    name = body.name.strip()[:120]
    if not name:
        raise HTTPException(400, "name is required")
    if body.content and body.png_base64:
        raise HTTPException(400, "provide content or png_base64, not both")
    raw_content = body.content
    if body.png_base64:
        try:
            import base64

            raw_content = extract_card_from_png(base64.b64decode(body.png_base64))
        except (ValueError, TypeError):
            raise HTTPException(400, "png_base64 is not valid base64") from None
        if not raw_content:
            raise HTTPException(400, "PNG 中未找到 SillyTavern 角色卡（chara/ccv3 chunk）")
    if not raw_content or not raw_content.strip():
        raise HTTPException(400, "content is required")
    content = raw_content.strip()[:20000]
    fmt = detect_format(content)
    exists = db.scalar(
        select(CharacterCard).where(CharacterCard.user_id == user_id, CharacterCard.name == name)
    )
    if exists:
        # 同名导入视为更新：保留激活状态与所有权
        exists.format = fmt
        exists.content_encrypted = encrypt_card_content(content)
        card = exists
    else:
        card = CharacterCard(
            user_id=user_id, name=name, format=fmt, content_encrypted=encrypt_card_content(content)
        )
        db.add(card)
        db.flush()
    db.commit()
    return {
        "ok": True,
        "card": {
            "id": card.id,
            "name": card.name,
            "format": card.format,
            "active": card.active,
            "created_at": card.created_at,
            "updated_at": card.updated_at,
        },
    }


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


@app.get("/api/admin/users/{user_id}/presets", dependencies=[Depends(admin)])
def user_presets(user_id: str, db: Session = Depends(db_dep)):
    """预设元数据：只暴露名称与更新时间，不暴露快照内容。"""
    if not db.get(User, user_id):
        raise HTTPException(404, "user not found")
    rows = db.scalars(
        select(Preset).where(Preset.user_id == user_id).order_by(Preset.created_at)
    )
    return [
        {
            "id": p.id,
            "name": p.name,
            "created_at": p.created_at,
            "updated_at": p.updated_at,
        }
        for p in rows
    ]


@app.get("/api/admin/users/{user_id}/providers", dependencies=[Depends(admin)])
def user_providers(user_id: str, db: Session = Depends(db_dep)):
    """BYOK 元数据：绝不返回 api_key_encrypted 或任何密钥字段。"""
    if not db.get(User, user_id):
        raise HTTPException(404, "user not found")
    rows = db.scalars(
        select(UserProvider).where(UserProvider.user_id == user_id).order_by(UserProvider.created_at)
    )
    return [
        {
            "id": p.id,
            "provider_key": p.provider_key,
            "base_url": p.base_url,
            "models": p.models,
            "is_default": p.is_default,
            "created_at": p.created_at,
            "updated_at": p.updated_at,
        }
        for p in rows
    ]


@app.get("/api/admin/users/{user_id}/detail", dependencies=[Depends(admin)])
def user_detail(user_id: str, db: Session = Depends(db_dep)):
    """用户详情统计：会话/记忆/媒体/今日用量，均为聚合数字。"""
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    conversations = db.scalar(
        select(func.count()).select_from(Conversation).where(Conversation.user_id == user_id)
    )
    memories = db.scalar(
        select(func.count()).select_from(Memory).where(Memory.user_id == user_id)
    )
    media = db.scalar(
        select(func.count()).select_from(MediaAsset)
        .join(Message, Message.id == MediaAsset.message_id)
        .where(Message.user_id == user_id)
    )
    tokens_today = db.scalar(
        select(func.coalesce(func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens), 0))
        .where(UsageRecord.user_id == user_id, UsageRecord.created_at >= today)
    )
    return {
        "user_id": user_id,
        "conversations": conversations or 0,
        "memories": memories or 0,
        "media_assets": media or 0,
        "tokens_today": int(tokens_today or 0),
    }


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
