from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import cards as card_service
from . import presets as preset_service
from .config import settings
from .models import CharacterCard, Conversation, Memory, Message, UsageRecord, User, UserSettings
from .policy import resolve_user_mode
from .security import decrypt_secret, encrypt_secret


@dataclass
class CommandResult:
    handled: bool
    reply: str = ""


HELP = """可用命令：
/help 查看帮助
/status 查看当前模式、角色卡、模型与参数
/models 查看可用模型
/model use <模型名> 切换模型
/persona show 查看人设
/persona set <内容> 设置人设
/persona reset 恢复默认人设
/temperature <0-2> 设置温度
/max-tokens <128-8192> 设置最大输出
/context <2-100> 设置上下文条数
/card list 列出角色卡
/card new <名称> 新建角色卡
/card use <名称> 切换角色卡
/card set <内容> 更新当前卡（支持 SOUL.md 或 SillyTavern JSON）
/card show [名称] 查看角色卡内容
/card export [名称] 导出角色卡文本
/card delete <名称> 删除角色卡
/preset list 列出预设
/preset save <名称> 保存当前配置为预设
/preset use <名称> 应用预设
/preset delete <名称> 删除预设
/new 开始新会话
/clear confirm 清除自己的聊天记录
/memory on|off|list|add <内容>|delete <序号>|clear confirm 管理私有记忆
/usage 查看今日用量"""

ALLOWED_MODELS = ["deepseek-chat", "deepseek-reasoner"]


def get_user_settings(db: Session, user_id: str) -> UserSettings:
    row = db.get(UserSettings, user_id)
    if not row:
        row = UserSettings(user_id=user_id)
        db.add(row)
        db.flush()
    return row


def execute(db: Session, user_id: str, text: str) -> CommandResult:
    if not text.startswith("/"):
        return CommandResult(False)
    parts = text.strip().split(maxsplit=2)
    cmd = parts[0].lower()
    user_settings = get_user_settings(db, user_id)
    user = db.get(User, user_id)

    if cmd == "/help":
        return CommandResult(True, HELP)
    if cmd == "/models":
        return CommandResult(True, "可用模型：\n" + "\n".join(f"- {item}" for item in ALLOWED_MODELS))
    if cmd == "/status":
        temperature = user_settings.temperature if user_settings.temperature is not None else 0.7
        memory_status = "开启" if user_settings.memory_enabled else "关闭"
        mode = resolve_user_mode(db, user)
        mode_text = {
            "single": "单用户",
            "self_service": "用户自足",
            "managed": "统一管理",
        }.get(mode, mode)
        active_card = (
            db.get(CharacterCard, user_settings.active_card_id) if user_settings.active_card_id else None
        )
        card_text = f"角色卡：{active_card.name}" if active_card else "角色卡：无"
        provider_text = "用户自带" if (user_settings.provider_key or "").startswith("byok:") else "平台"
        return CommandResult(
            True,
            f"模式：{mode_text}\n"
            f"{card_text}\n"
            f"供应商：{provider_text}\n"
            f"模型：{user_settings.model or settings.default_model}\n"
            f"温度：{temperature}\n"
            f"最大输出：{user_settings.max_tokens or 2048}\n"
            f"上下文：{user_settings.context_messages}\n"
            f"记忆：{memory_status}",
        )
    if cmd == "/model" and len(parts) >= 3 and parts[1].lower() == "use":
        if parts[2] not in ALLOWED_MODELS:
            return CommandResult(True, "该模型不可用。发送 /models 查看列表。")
        user_settings.model = parts[2]
        return CommandResult(True, f"模型已切换为 {user_settings.model}")
    if cmd == "/persona":
        action = parts[1].lower() if len(parts) > 1 else "show"
        if action == "show":
            return CommandResult(True, user_settings.system_prompt or settings.default_system_prompt)
        if action == "reset":
            user_settings.system_prompt = None
            return CommandResult(True, "人设已恢复平台默认值。")
        if action == "set" and len(parts) >= 3:
            user_settings.system_prompt = parts[2][:8000]
            return CommandResult(True, "人设已更新，仅对你的会话生效。")
        return CommandResult(True, "用法：/persona show | set <内容> | reset")
    if cmd in {"/temperature", "/max-tokens", "/context"}:
        return _set_parameter(user_settings, cmd, parts)
    if cmd == "/new":
        db.query(Conversation).filter_by(user_id=user_id, status="active").update({"status": "closed"})
        return CommandResult(True, "新的会话已经开始。")
    if cmd == "/clear":
        if len(parts) > 1 and parts[1].lower() == "confirm":
            db.execute(delete(Message).where(Message.user_id == user_id))
            db.execute(delete(Conversation).where(Conversation.user_id == user_id))
            return CommandResult(True, "你的聊天记录已清除。")
        return CommandResult(True, "此操作只清除你自己的聊天记录。确认请发送 /clear confirm")
    if cmd == "/memory":
        return _memory_command(db, user_id, parts, user_settings)
    if cmd == "/usage":
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        total = db.scalar(
            select(
                func.coalesce(func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens), 0)
            ).where(UsageRecord.user_id == user_id, UsageRecord.created_at >= start)
        )
        quota = user_settings.daily_token_quota or settings.user_daily_token_quota
        return CommandResult(True, f"今日已使用约 {total} tokens；额度 {quota}。")
    if cmd == "/card":
        return _card_command(db, user_id, parts, user_settings)
    if cmd == "/preset":
        return _preset_command(db, user_id, parts, user_settings)
    return CommandResult(True, "未知命令。发送 /help 查看可用命令。")


def _set_parameter(user_settings: UserSettings, cmd: str, parts: list[str]) -> CommandResult:
    if len(parts) < 2:
        return CommandResult(True, "缺少参数。")
    try:
        if cmd == "/temperature":
            value = float(parts[1])
            assert 0 <= value <= 2
            user_settings.temperature = value
        elif cmd == "/max-tokens":
            value = int(parts[1])
            assert 128 <= value <= 8192
            user_settings.max_tokens = value
        else:
            value = int(parts[1])
            assert 2 <= value <= 100
            user_settings.context_messages = value
        return CommandResult(True, "参数已更新。")
    except (ValueError, AssertionError):
        return CommandResult(True, "参数超出允许范围。")


def memory_text(memory: Memory) -> str:
    """读取记忆明文：优先解密新格式，旧明文回退。"""
    if memory.content_encrypted:
        try:
            return decrypt_secret(memory.content_encrypted)
        except Exception:  # noqa: BLE001 - 旧明文数据或密钥轮换时回退读取
            return memory.content
    return memory.content


def _memory_command(
    db: Session, user_id: str, parts: list[str], user_settings: UserSettings
) -> CommandResult:
    action = parts[1].lower() if len(parts) > 1 else "list"
    if action in {"on", "off"}:
        user_settings.memory_enabled = action == "on"
        status = "开启" if user_settings.memory_enabled else "关闭"
        return CommandResult(True, f"长期记忆已{status}。")
    rows = list(db.scalars(select(Memory).where(Memory.user_id == user_id).order_by(Memory.created_at)))
    if action == "list":
        listing = (
            "记忆为空。"
            if not rows
            else "\n".join(f"{i + 1}. {memory_text(row)}" for i, row in enumerate(rows))
        )
        return CommandResult(True, listing)
    if action == "add" and len(parts) >= 3:
        db.add(
            Memory(user_id=user_id, content="", content_encrypted=encrypt_secret(parts[2][:2000]))
        )
        return CommandResult(True, "这条记忆已保存。")
    if action == "delete" and len(parts) >= 3:
        try:
            row = rows[int(parts[2]) - 1]
            db.delete(row)
            return CommandResult(True, "记忆已删除。")
        except (ValueError, IndexError):
            return CommandResult(True, "没有这条记忆。")
    if action == "clear" and len(parts) >= 3 and parts[2].lower() == "confirm":
        db.execute(delete(Memory).where(Memory.user_id == user_id))
        return CommandResult(True, "你的长期记忆已清空。")
    return CommandResult(True, "用法：/memory on|off|list|add <内容>|delete <序号>|clear confirm")


def _active_card(db: Session, user_settings: UserSettings) -> CharacterCard | None:
    if not user_settings.active_card_id:
        return None
    return db.get(CharacterCard, user_settings.active_card_id)


def _set_active_card(db: Session, user_settings: UserSettings, card: CharacterCard) -> None:
    # 同一用户同一时刻只激活一张卡
    db.execute(
        CharacterCard.__table__.update()
        .where(CharacterCard.user_id == user_settings.user_id, CharacterCard.active.is_(True))
        .values(active=False)
    )
    card.active = True
    user_settings.active_card_id = card.id


def _card_command(
    db: Session, user_id: str, parts: list[str], user_settings: UserSettings
) -> CommandResult:
    action = parts[1].lower() if len(parts) > 1 else "list"
    if action == "list":
        rows = list(
            db.scalars(
                select(CharacterCard)
                .where(CharacterCard.user_id == user_id)
                .order_by(CharacterCard.created_at)
            )
        )
        if not rows:
            return CommandResult(True, "还没有角色卡。发送 /card new <名称> 创建一张。")
        active = _active_card(db, user_settings)
        listing = "\n".join(
            f"{'● ' if card.id == (active.id if active else None) else '○ '}{card.name}"
            f"（{card.format}）"
            for card in rows
        )
        return CommandResult(True, listing)
    if action == "new" and len(parts) >= 3:
        name = parts[2].strip()[:120]
        exists = db.scalar(
            select(CharacterCard).where(
                CharacterCard.user_id == user_id, CharacterCard.name == name
            )
        )
        if exists:
            return CommandResult(True, f"角色卡「{name}」已存在。")
        card = CharacterCard(user_id=user_id, name=name, format="soul_md", content_encrypted=None)
        db.add(card)
        db.flush()
        _set_active_card(db, user_settings, card)
        return CommandResult(True, f"角色卡「{name}」已创建并激活。发送 /card set <内容> 写入人格（SOUL.md 或 SillyTavern JSON）。")
    if action == "use" and len(parts) >= 3:
        card = db.scalar(
            select(CharacterCard).where(
                CharacterCard.user_id == user_id, CharacterCard.name == parts[2].strip()
            )
        )
        if not card:
            return CommandResult(True, "没有这张卡。发送 /card list 查看。")
        _set_active_card(db, user_settings, card)
        return CommandResult(True, f"已切换到角色卡「{card.name}」。")
    if action == "set" and len(parts) >= 3:
        card = _active_card(db, user_settings)
        if not card:
            return CommandResult(True, "当前没有激活的角色卡。先 /card new <名称> 创建一张。")
        content = parts[2][:20000]
        fmt = card_service.detect_format(content)
        card.format = fmt
        card.content_encrypted = card_service.encrypt_card_content(content)
        return CommandResult(True, f"角色卡「{card.name}」已更新（{fmt} 格式）。")
    if action == "show":
        card = None
        if len(parts) >= 3:
            card = db.scalar(
                select(CharacterCard).where(
                    CharacterCard.user_id == user_id, CharacterCard.name == parts[2].strip()
                )
            )
        else:
            card = _active_card(db, user_settings)
        if not card:
            return CommandResult(True, "没有这张卡。发送 /card list 查看。")
        content = (
            card_service.decrypt_card_content(card.content_encrypted)
            if card.content_encrypted
            else "（空）"
        )
        return CommandResult(True, f"【{card.name}】{card.format}\n{content[:2000]}")
    if action == "export":
        card = None
        if len(parts) >= 3:
            card = db.scalar(
                select(CharacterCard).where(
                    CharacterCard.user_id == user_id, CharacterCard.name == parts[2].strip()
                )
            )
        else:
            card = _active_card(db, user_settings)
        if not card or not card.content_encrypted:
            return CommandResult(True, "没有可导出的内容。")
        content = card_service.decrypt_card_content(card.content_encrypted)
        return CommandResult(True, card_service.export_card_text(card.format, content)[:2000])
    if action == "delete" and len(parts) >= 3:
        card = db.scalar(
            select(CharacterCard).where(
                CharacterCard.user_id == user_id, CharacterCard.name == parts[2].strip()
            )
        )
        if not card:
            return CommandResult(True, "没有这张卡。")
        if user_settings.active_card_id == card.id:
            user_settings.active_card_id = None
        db.delete(card)
        return CommandResult(True, f"角色卡「{card.name}」已删除。")
    return CommandResult(True, "用法：/card list | new <名称> | use <名称> | set <内容> | show [名称] | export [名称] | delete <名称>")


def _preset_command(
    db: Session, user_id: str, parts: list[str], user_settings: UserSettings
) -> CommandResult:
    action = parts[1].lower() if len(parts) > 1 else ""
    if action == "list":
        rows = preset_service.list_presets(db, user_id)
        if not rows:
            return CommandResult(True, "还没有预设。发送 /preset save <名称> 保存当前配置。")
        return CommandResult(True, "\n".join(f"- {row.name}" for row in rows))
    if action == "save" and len(parts) >= 3:
        name = parts[2].strip()[:120]
        preset_service.save_preset(db, user_id, name, preset_service.snapshot_settings(user_settings))
        return CommandResult(True, f"预设「{name}」已保存。")
    if action == "use" and len(parts) >= 3:
        preset = preset_service.get_preset(db, user_id, parts[2].strip())
        if not preset:
            return CommandResult(True, "没有这个预设。")
        preset_service.apply_snapshot(user_settings, preset.config)
        return CommandResult(True, f"预设「{preset.name}」已应用。")
    if action == "delete" and len(parts) >= 3:
        if preset_service.delete_preset(db, user_id, parts[2].strip()):
            return CommandResult(True, f"预设「{parts[2].strip()}」已删除。")
        return CommandResult(True, "没有这个预设。")
    return CommandResult(True, "用法：/preset list | save <名称> | use <名称> | delete <名称>")
