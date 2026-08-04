import logging
from datetime import UTC, datetime

from sqlalchemy import func, select

from .commands import execute
from .config import settings
from .db import SessionLocal
from .models import (
    ChannelIdentity,
    ChannelState,
    Conversation,
    Memory,
    Message,
    MessageDirection,
    MessageStatus,
    UsageRecord,
    User,
    UserSettings,
)
from .providers import provider_for
from .security import decrypt_secret, encrypt_secret, external_id_hash
from .tasks import add_message_task
from .wecom import client

log = logging.getLogger(__name__)


def resolve_user(db, external_id: str, account_id: str) -> User:
    digest = external_id_hash(external_id)
    identity = db.scalar(
        select(ChannelIdentity).where(
            ChannelIdentity.channel == "wecom_kf",
            ChannelIdentity.account_id == account_id,
            ChannelIdentity.external_id_hash == digest,
        )
    )
    if identity:
        return identity.user
    user = User()
    db.add(user)
    db.flush()
    db.add(
        ChannelIdentity(
            user_id=user.id,
            channel="wecom_kf",
            account_id=account_id,
            external_id_hash=digest,
            external_id_encrypted=encrypt_secret(external_id),
        )
    )
    db.add(UserSettings(user_id=user.id))
    db.flush()
    return user


def active_conversation(db, user_id: str) -> Conversation:
    row = db.scalar(
        select(Conversation)
        .where(Conversation.user_id == user_id, Conversation.status == "active")
        .order_by(Conversation.created_at.desc())
    )
    if not row:
        row = Conversation(user_id=user_id)
        db.add(row)
        db.flush()
    return row


async def sync_wecom_messages(callback_token: str, open_kfid: str) -> None:
    db = SessionLocal()
    account_id = open_kfid or settings.wecom_open_kfid
    state_key = f"wecom_cursor:{account_id}"
    try:
        state = db.get(ChannelState, state_key)
        cursor = state.value if state else ""
        while True:
            result = await client.sync(callback_token, account_id, cursor)
            if result.get("errcode") != 0:
                raise RuntimeError(f"sync_msg failed: {result.get('errcode')} {result.get('errmsg')}")
            for item in result.get("msg_list", []):
                ingest(db, item)
            # 消息、游标与待处理任务在同一数据库事务中提交。
            # Redis 只负责唤醒 Worker；即使通知失败，定时扫描仍会处理 Outbox。
            queued_ids = [
                message_id
                for (message_id,) in db.execute(
                    select(Message.id).where(
                        Message.status == MessageStatus.queued,
                        Message.metadata_json["open_kfid"].as_string() == account_id,
                    )
                )
            ]
            for message_id in queued_ids:
                add_message_task(db, message_id)
            cursor = result.get("next_cursor", cursor)
            if state:
                state.value = cursor
            else:
                state = ChannelState(key=state_key, value=cursor)
                db.add(state)
            db.commit()
            if not result.get("has_more"):
                break
    finally:
        db.close()


def ingest(db, item: dict) -> None:
    msgid = item.get("msgid")
    origin = int(item.get("origin", 0) or 0)
    msgtype = item.get("msgtype", "")
    if not msgid:
        return
    exists = db.scalar(
        select(Message.id).where(Message.channel == "wecom_kf", Message.external_message_id == msgid)
    )
    if exists:
        return
    external_id = item.get("external_userid", "")
    open_kfid = item.get("open_kfid", "")
    user = resolve_user(db, external_id, open_kfid) if external_id else None
    conversation = active_conversation(db, user.id) if user else None
    content = (item.get("text") or {}).get("content") if msgtype == "text" else None
    should_reply = origin == 3 and msgtype == "text" and user and not user.is_blocked
    row = Message(
        conversation_id=conversation.id if conversation else None,
        user_id=user.id if user else None,
        channel="wecom_kf",
        external_message_id=msgid,
        direction=MessageDirection.inbound,
        message_type=msgtype,
        content=content,
        status=MessageStatus.queued if should_reply else MessageStatus.ignored,
        metadata_json={"origin": origin, "open_kfid": open_kfid},
    )
    db.add(row)
    db.flush()


async def process_message(message_id: str) -> None:
    db = SessionLocal()
    row = db.get(Message, message_id)
    if not row or row.status not in {
        MessageStatus.queued,
        MessageStatus.failed,
        MessageStatus.processing,
    }:
        db.close()
        return
    row.status = MessageStatus.processing
    db.commit()
    try:
        user_settings = db.get(UserSettings, row.user_id) or UserSettings(user_id=row.user_id)
        conversation = db.get(Conversation, row.conversation_id)
        command = execute(db, row.user_id, row.content or "")
        if command.handled:
            answer = command.reply
        else:
            answer = await _complete_ai(db, row, conversation, user_settings)

        identity = db.scalar(
            select(ChannelIdentity).where(
                ChannelIdentity.user_id == row.user_id,
                ChannelIdentity.channel == "wecom_kf",
            )
        )
        external_id = decrypt_secret(identity.external_id_encrypted)
        open_kfid = (row.metadata_json or {}).get("open_kfid") or settings.wecom_open_kfid
        answer = answer or "暂时没有生成可发送的内容。"
        sent_id = await client.send_text(open_kfid, external_id, answer)
        db.add(
            Message(
                conversation_id=row.conversation_id,
                user_id=row.user_id,
                channel="wecom_kf",
                external_message_id=sent_id or f"local:{row.id}",
                direction=MessageDirection.outbound,
                message_type="text",
                content=answer,
                status=MessageStatus.sent,
                metadata_json={"reply_to": row.external_message_id},
            )
        )
        row.status = MessageStatus.sent
        row.error = None
        db.commit()
    except Exception as exc:
        row.status = MessageStatus.failed
        row.error = str(exc)[:1000]
        db.commit()
        log.exception("处理消息失败 id=%s", message_id)
        raise
    finally:
        db.close()


async def _complete_ai(db, row: Message, conversation: Conversation, user_settings: UserSettings) -> str:
    # 未配置模型凭据时保持网关可用，并向用户返回明确状态，而不是制造失败任务。
    if not settings.openai_compatible_api_key:
        return settings.unconfigured_model_message

    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    used = db.scalar(
        select(func.coalesce(func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens), 0)).where(
            UsageRecord.user_id == row.user_id, UsageRecord.created_at >= start
        )
    )
    quota = user_settings.daily_token_quota or settings.user_daily_token_quota
    if used >= quota:
        return "今天的模型使用额度已经用完，请明天再来。"

    history = list(
        db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.status.in_([MessageStatus.sent, MessageStatus.processing]),
            )
            .order_by(Message.created_at.desc())
            .limit(user_settings.context_messages or 20)
        )
    )[::-1]
    memories = []
    if user_settings.memory_enabled:
        memories = list(
            db.scalars(
                select(Memory).where(Memory.user_id == row.user_id).order_by(Memory.created_at).limit(50)
            )
        )
    system_prompt = user_settings.system_prompt or settings.default_system_prompt
    if memories:
        system_prompt += "\n\n只在相关时参考这些用户私有记忆：\n" + "\n".join(
            f"- {memory.content}" for memory in memories
        )
    prompts = [{"role": "system", "content": system_prompt}]
    prompts.extend(
        {
            "role": "user" if message.direction == MessageDirection.inbound else "assistant",
            "content": message.content or "",
        }
        for message in history
    )
    provider_name = user_settings.provider or settings.default_provider
    model = user_settings.model or settings.default_model
    result = await provider_for(provider_name).complete(
        prompts,
        model,
        user_settings.temperature if user_settings.temperature is not None else 0.7,
        user_settings.max_tokens or 2048,
    )
    db.add(
        UsageRecord(
            user_id=row.user_id,
            provider=provider_name,
            model=model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
    )
    return result.content
