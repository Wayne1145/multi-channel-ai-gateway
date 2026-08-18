import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import cards as card_service
from .channels import ChannelMessage, OutgoingMessage, registry
from .commands import execute, memory_text
from .config import settings
from .db import SessionLocal
from .media import record_media_items
from .models import (
    ChannelIdentity,
    ChannelState,
    CharacterCard,
    Conversation,
    Memory,
    Message,
    MessageDirection,
    MessageStatus,
    UsageRecord,
    User,
    UserProvider,
    UserSettings,
)
from .policy import get_command_decision, normalize_command
from .providers import provider_for
from .redaction import redact_error
from .runtime_settings import get_effective_value, get_runtime_value
from .security import decrypt_secret, encrypt_secret, external_id_hash
from .tasks import add_message_task
from .wecom import client

log = logging.getLogger(__name__)


def resolve_user(db, external_id: str, account_id: str, channel: str = "wecom_kf") -> User:
    digest = external_id_hash(external_id)
    identity = db.scalar(
        select(ChannelIdentity).where(
            ChannelIdentity.channel == channel,
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
            channel=channel,
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


def ingest_channel_message(db, incoming: ChannelMessage) -> Message | None:
    """将任意文本渠道消息按统一身份、会话和 Outbox 语义入库。

    非文本消息保留为 ignored；同一渠道实例内重复 external_message_id 不会生成第二条任务。
    渠道适配器只需构造 ChannelMessage，不能绕过这条隔离路径。
    媒体条目只落 MediaAsset 元数据，不进 metadata_json（避免 URL/凭据滞留）。
    """
    if not incoming.external_message_id:
        raise ValueError("渠道消息缺少 external_message_id")
    exists = db.scalar(
        select(Message.id).where(
            Message.channel == incoming.channel,
            Message.channel_instance_id == incoming.instance_id,
            Message.external_message_id == incoming.external_message_id,
        )
    )
    if exists:
        return None
    content = incoming.content
    if incoming.message_type == "text" and content:
        content = content[: int(get_effective_value(db, "message_max_chars", channel=incoming.channel))]
    user = resolve_user(db, incoming.sender_id, incoming.instance_id, incoming.channel)
    conversation = active_conversation(db, user.id)
    media = [m for m in (incoming.media or []) if isinstance(m, dict)]
    should_reply = (
        incoming.message_type == "text"
        and bool(content)
        and not user.is_blocked
    )
    row = Message(
        conversation_id=conversation.id,
        user_id=user.id,
        channel=incoming.channel,
        channel_instance_id=incoming.instance_id,
        external_message_id=incoming.external_message_id,
        direction=MessageDirection.inbound,
        message_type=incoming.message_type,
        content=content,
        status=MessageStatus.queued if should_reply else MessageStatus.ignored,
        metadata_json={
            "instance_id": incoming.instance_id,
            "sender_id": incoming.sender_id,
            "media_count": len(media),
            "media_types": sorted({m.get("media_type") or m.get("type") or "" for m in media}),
        },
    )
    db.add(row)
    db.flush()
    if media:
        record_media_items(db, row.id, incoming.channel, media)
    if row.status == MessageStatus.queued:
        add_message_task(db, row.id)
    return row


def ingest(db, item: dict) -> None:
    msgid = item.get("msgid")
    origin = int(item.get("origin", 0) or 0)
    msgtype = item.get("msgtype", "")
    if not msgid:
        return
    exists = db.scalar(
        select(Message.id).where(
            Message.channel == "wecom_kf",
            Message.channel_instance_id == str(item.get("open_kfid", "")),
            Message.external_message_id == msgid,
        )
    )
    if exists:
        return
    external_id = item.get("external_userid", "")
    open_kfid = item.get("open_kfid", "")
    user = resolve_user(db, external_id, open_kfid) if external_id else None
    conversation = active_conversation(db, user.id) if user else None
    content = (item.get("text") or {}).get("content") if msgtype == "text" else None
    if content:
        content = content[: int(get_effective_value(db, "message_max_chars", channel="wecom_kf"))]
    # 企微媒体消息：image/voice/file 携带 media_id 等定位信息，仅记录安全元数据
    media: list[dict] = []
    if msgtype in {"image", "voice", "file"}:
        media = [{"media_type": msgtype, "media_id": item.get("media_id"), "mime": item.get("format")}]
    should_reply = origin == 3 and msgtype == "text" and user and not user.is_blocked
    row = Message(
        conversation_id=conversation.id if conversation else None,
        user_id=user.id if user else None,
        channel="wecom_kf",
        channel_instance_id=open_kfid,
        external_message_id=msgid,
        direction=MessageDirection.inbound,
        message_type=msgtype,
        content=content,
        status=MessageStatus.queued if should_reply else MessageStatus.ignored,
        metadata_json={
            "origin": origin,
            "open_kfid": open_kfid,
            "media_count": len(media),
            "media_types": sorted({m.get("media_type") or "" for m in media}),
        },
    )
    db.add(row)
    db.flush()
    if media:
        record_media_items(db, row.id, "wecom_kf", media)


def resolve_provider(db, user_settings: UserSettings):
    """返回 (provider_name, base_url, api_key)。用户 BYOK 优先，否则平台默认。"""
    provider_key = user_settings.provider_key or ""
    if provider_key.startswith("byok:"):
        provider = db.scalar(
            select(UserProvider).where(
                UserProvider.id == provider_key.split(":", 1)[1],
                UserProvider.user_id == user_settings.user_id,
            )
        )
        if provider and provider.api_key_encrypted:
            try:
                return (
                    provider.provider_key,
                    provider.base_url,
                    decrypt_secret(provider.api_key_encrypted),
                )
            except Exception:
                log.exception("解密用户 BYOK 密钥失败 user=%s", user_settings.user_id)
        raise RuntimeError("用户 BYOK 配置已失效，请重新选择或配置供应商")
    return (
        settings.default_provider,
        settings.openai_compatible_base_url,
        settings.openai_compatible_api_key,
    )


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
        bind_result = _handle_bind_command(db, row)
        if bind_result is not None:
            db.add(
                Message(
                    conversation_id=conversation.id if conversation else None,
                    user_id=row.user_id,
                    channel=row.channel,
                    channel_instance_id=(row.metadata_json or {}).get("instance_id") or "",
                    external_message_id=f"local:{row.id}",
                    direction=MessageDirection.outbound,
                    message_type="text",
                    content=bind_result,
                    status=MessageStatus.sent,
                    metadata_json={"reply_to": row.external_message_id},
                )
            )
            row.status = MessageStatus.sent
            db.commit()
            db.close()
            return
        text = row.content or ""
        answer = None
        blocked_answer = None
        command = None
        if text.startswith("/"):
            decision = get_command_decision(
                db, row.user_id, row.channel, normalize_command(text) or ""
            )
            if decision.allowed:
                command = execute(db, row.user_id, text)
            elif decision.silent_block:
                if decision.blocked_strategy == "ignore":
                    # 静默忽略：不回复、不转 AI，像没收到一样
                    row.status = MessageStatus.ignored
                    row.error = None
                    db.commit()
                    db.close()
                    return
                # redirect_to_ai：当作普通消息交给 AI
            else:
                blocked_answer = "该指令在当前模式下不可用。"
        else:
            command = execute(db, row.user_id, text)
        if blocked_answer is not None:
            answer = blocked_answer
        elif command is not None and command.handled:
            answer = command.reply
        elif bool(get_runtime_value(db, "maintenance_mode")) and not settings.single_user_mode:
            answer = str(get_runtime_value(db, "maintenance_message"))
        else:
            answer = await _complete_ai(db, row, conversation, user_settings)

        metadata = dict(row.metadata_json or {})
        if row.channel == "wecom_kf":
            account_id = metadata.get("open_kfid") or settings.wecom_open_kfid
        else:
            account_id = metadata.get("instance_id")
        if not account_id:
            raise RuntimeError("入站消息缺少渠道实例标识")
        identity = db.scalar(
            select(ChannelIdentity).where(
                ChannelIdentity.user_id == row.user_id,
                ChannelIdentity.channel == row.channel,
                ChannelIdentity.account_id == account_id,
            )
        )
        if identity is None:
            raise RuntimeError("找不到与入站渠道实例匹配的用户身份")
        external_id = decrypt_secret(identity.external_id_encrypted)
        answer = (answer or "").strip()
        # 空回复不能作为成功结果发送。它通常来自供应商的长推理/临时空响应；
        # 抛出异常交给 Outbox 重试，避免向用户展示误导性的兜底文案。
        if not answer:
            raise RuntimeError("未生成可发送的回复内容，将按任务策略重试")

        # 企微 send_msg 目前没有调用方可提供的幂等键。外部调用成功后若进程在
        # 本地提交前崩溃，无法区分“未发出”和“已送达”。因此先持久化投递栅栏：
        # 发生不确定结果时停止自动重发，宁可进入人工死信，也不能重复回复用户。
        metadata = dict(row.metadata_json or {})
        if metadata.get("reply_dispatch") == "started":
            raise RuntimeError("回复投递状态未知，已停止自动重发以避免重复消息")
        metadata["reply_dispatch"] = "started"
        row.metadata_json = metadata
        db.commit()

        chunk_limit = int(get_runtime_value(db, "message_chunk_chars"))
        chunks = _split_reply(answer, chunk_limit) if len(answer) > chunk_limit else [answer]
        if row.channel == "wecom_kf":
            sent_id = ""
            for chunk in chunks:
                sent_id = await client.send_text(account_id, external_id, chunk)
                db.add(
                    Message(
                        conversation_id=row.conversation_id,
                        user_id=row.user_id,
                        channel=row.channel,
                        channel_instance_id=account_id,
                        external_message_id=sent_id or f"local:{row.id}",
                        direction=MessageDirection.outbound,
                        message_type="text",
                        content=chunk,
                        status=MessageStatus.sent,
                        metadata_json={"reply_to": row.external_message_id, "chunk": len(chunks) > 1},
                    )
                )
            outbound_media = list(metadata.get("media") or [])
            for media in outbound_media:
                media_msg_id = await client.send_media(account_id, external_id, media)
                db.add(
                    Message(
                        conversation_id=row.conversation_id,
                        user_id=row.user_id,
                        channel=row.channel,
                        channel_instance_id=account_id,
                        external_message_id=media_msg_id or f"local:{row.id}",
                        direction=MessageDirection.outbound,
                        message_type=str(media.get("media_type") or "media"),
                        content=None,
                        status=MessageStatus.sent,
                        metadata_json={"reply_to": row.external_message_id, "media": True},
                    )
                )
        else:
            adapter = registry.get(row.channel)
            sent_id = ""
            for chunk in chunks:
                sent_id = await adapter.send(
                    OutgoingMessage(
                        channel=row.channel,
                        instance_id=account_id,
                        to_sender_id=external_id,
                        text=chunk,
                        metadata={"reply_to": row.external_message_id},
                    )
                )
                db.add(
                    Message(
                        conversation_id=row.conversation_id,
                        user_id=row.user_id,
                        channel=row.channel,
                        channel_instance_id=account_id,
                        external_message_id=sent_id or f"local:{row.id}",
                        direction=MessageDirection.outbound,
                        message_type="text",
                        content=chunk,
                        status=MessageStatus.sent,
                        metadata_json={"reply_to": row.external_message_id, "chunk": len(chunks) > 1},
                    )
                )
            outbound_media = list(metadata.get("media") or [])
            for media in outbound_media:
                media_msg_id = await adapter.send_media(
                    OutgoingMessage(
                        channel=row.channel,
                        instance_id=account_id,
                        to_sender_id=external_id,
                        text="",
                        media=[media],
                        metadata={"reply_to": row.external_message_id},
                    )
                )
                db.add(
                    Message(
                        conversation_id=row.conversation_id,
                        user_id=row.user_id,
                        channel=row.channel,
                        channel_instance_id=account_id,
                        external_message_id=media_msg_id or f"local:{row.id}",
                        direction=MessageDirection.outbound,
                        message_type=str(media.get("media_type") or "media"),
                        content=None,
                        status=MessageStatus.sent,
                        metadata_json={"reply_to": row.external_message_id, "media": True},
                    )
                )
        metadata["reply_dispatch"] = "sent"
        row.metadata_json = metadata
        row.status = MessageStatus.sent
        row.error = None
        db.commit()
    except Exception as exc:
        # flush/commit 失败后 Session 必须先回滚，才能安全持久化失败状态。
        db.rollback()
        row = db.get(Message, message_id)
        if row is not None and row.status != MessageStatus.sent:
            row.status = MessageStatus.failed
            row.error = redact_error(exc, 1000)
            db.commit()
        log.error("处理消息失败 id=%s error=%s", message_id, redact_error(exc, 300))
        raise
    finally:
        db.close()


async def _complete_ai(db, row: Message, conversation: Conversation, user_settings: UserSettings) -> str:
    # BYOK 始终直连用户自己的供应商；其余用户优先走平台默认模型组，
    # 未配置模型组时兼容回退到原有 .env 单供应商。
    provider_name, base_url, api_key = resolve_provider(db, user_settings)
    is_byok = bool((user_settings.provider_key or "").startswith("byok:"))
    from .model_routing import active_routes

    selected_group_id = user_settings.model_group_id
    selected_routes = active_routes(db, selected_group_id) if selected_group_id else active_routes(db)
    if selected_group_id and not selected_routes:
        selected_group_id = None
        selected_routes = active_routes(db)
    use_model_group = not is_byok and bool(selected_routes)
    if not use_model_group and not api_key:
        return settings.unconfigured_model_message

    status = quota_status(db, row.user_id, user_settings)
    if status["exceeded"]:
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
    # 角色卡注入：激活的卡内容作为人格主体，用户自定义人设作为补充
    card_text = ""
    if user_settings.active_card_id:
        card = db.scalar(
            select(CharacterCard).where(
                CharacterCard.id == user_settings.active_card_id,
                CharacterCard.user_id == row.user_id,
            )
        )
        if card and card.content_encrypted:
            card_text = card_service.card_to_system_prompt(
                card.format, card_service.decrypt_card_content(card.content_encrypted)
            )
    if card_text:
        system_prompt = card_text
        if user_settings.system_prompt:
            system_prompt += "\n\n" + user_settings.system_prompt
    else:
        system_prompt = user_settings.system_prompt or settings.default_system_prompt
    if memories:
        system_prompt += "\n\n只在相关时参考这些用户私有记忆：\n" + "\n".join(
            f"- {memory_text(memory)}" for memory in memories
        )
    if bool(get_runtime_value(db, "kb_enabled")):
        from .knowledge import build_injection

        kb_text = build_injection(
            db,
            row.user_id,
            row.content or "",
            max_chunks=int(get_runtime_value(db, "kb_max_chunks")),
            chunk_chars=int(get_runtime_value(db, "kb_chunk_chars")),
        )
        if kb_text:
            system_prompt += (
                "\n\n以下是用户知识库中与问题相关的内容，回答时优先参考：\n" + kb_text
            )
    prompts = [{"role": "system", "content": system_prompt}]
    prompts.extend(
        {
            "role": "user" if message.direction == MessageDirection.inbound else "assistant",
            "content": message.content or "",
        }
        for message in history
    )
    model = user_settings.model or settings.default_model
    temperature = user_settings.temperature if user_settings.temperature is not None else 0.7
    max_tokens = user_settings.max_tokens or int(get_runtime_value(db, "default_max_tokens"))
    timeout = float(get_runtime_value(db, "request_timeout_seconds"))
    if use_model_group:
        from .model_routing import complete_with_routing

        result = await complete_with_routing(
            db,
            user_settings,
            prompts,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            group_id=selected_group_id,
        )
        provider_name = result.provider_name
        model = result.model
    else:
        result = await provider_for(
            provider_name,
            base_url,
            api_key,
            timeout=timeout,
        ).complete(prompts, model, temperature, max_tokens)
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


def _handle_bind_command(db: Session, row: Message) -> str | None:
    """处理跨渠道绑定指令 /bind；非绑定指令返回 None。"""
    content = (row.content or "").strip()
    if not content.startswith("/bind"):
        return None
    from .binding import create_bind_code, resolve_bind

    metadata = row.metadata_json or {}
    channel = row.channel
    account_id = metadata.get("instance_id") or (metadata.get("open_kfid") or "")
    identity = db.scalar(
        select(ChannelIdentity).where(
            ChannelIdentity.user_id == row.user_id,
            ChannelIdentity.channel == channel,
            ChannelIdentity.account_id == account_id,
        )
    )
    if identity is None:
        return "绑定需要先通过该渠道发送一条消息以识别身份。"
    external_id = decrypt_secret(identity.external_id_encrypted)
    parts = content.split()
    if len(parts) == 1:
        code = create_bind_code(db, row.user_id)
        return (
            f"跨渠道绑定：请在另一个微信渠道发送 /bind {code} 完成合并。"
            f"绑定码 10 分钟内有效，仅限一次。"
        )
    if len(parts) == 2:
        result = resolve_bind(
            db,
            parts[1],
            user_id=row.user_id,
            channel=channel,
            account_id=account_id,
            external_id=external_id,
        )
        return result["message"]
    return "用法：/bind 获取绑定码；/bind <码> 完成绑定。"


def _split_reply(text: str, limit: int) -> list[str]:
    """把长回复按字符数拆片；优先在换行/句末标点处断开，避免切断语义。

    每片长度严格不超过 limit；无断点时按 limit 硬切。
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    break_chars = ("。", "！", "？", "；", "，", ".", "!", "?", ";", ",")
    while len(rest) > limit:
        newline = rest.rfind("\n", 0, limit)
        if newline > limit // 2:
            cut = newline
        else:
            cut = max((rest.rfind(c, 0, limit) for c in break_chars), default=-1)
            if cut <= limit // 2:
                cut = limit
        chunk = rest[: cut + 1] if cut < limit else rest[:limit]
        chunk = chunk.strip()
        if chunk:
            chunks.append(chunk)
        rest = rest[len(chunk) :].lstrip() if chunk else rest[limit:]
        if not rest:
            break
    if rest:
        chunks.append(rest.strip())
    return [c for c in chunks if c]


def quota_status(db, user_id: str, user_settings) -> dict:
    """返回用户当日配额状态；配额开关关闭时永不超限。"""
    if not bool(get_runtime_value(db, "daily_quota_enabled")):
        return {"enabled": False, "quota": 0, "used": 0, "remaining": 0, "exceeded": False}
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    used = int(
        db.scalar(
            select(func.coalesce(func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens), 0)).where(
                UsageRecord.user_id == user_id, UsageRecord.created_at >= start
            )
        )
        or 0
    )
    quota = int(
        (user_settings.daily_token_quota if user_settings and user_settings.daily_token_quota else None)
        or get_effective_value(db, "user_daily_token_quota", user_id=user_id)
    )
    return {
        "enabled": True,
        "quota": quota,
        "used": used,
        "remaining": max(quota - used, 0),
        "exceeded": used >= quota,
    }
