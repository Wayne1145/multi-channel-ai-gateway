from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import Conversation, Memory, Message, UsageRecord, UserSettings


@dataclass
class CommandResult:
    handled: bool
    reply: str = ""


HELP = """可用命令：
/help 查看帮助
/status 查看当前模型与参数
/models 查看可用模型
/model use <模型名> 切换模型
/persona show 查看人设
/persona set <内容> 设置人设
/persona reset 恢复默认人设
/temperature <0-2> 设置温度
/max-tokens <128-8192> 设置最大输出
/context <2-100> 设置上下文条数
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

    if cmd == "/help":
        return CommandResult(True, HELP)
    if cmd == "/models":
        return CommandResult(True, "可用模型：\n" + "\n".join(f"- {item}" for item in ALLOWED_MODELS))
    if cmd == "/status":
        temperature = user_settings.temperature if user_settings.temperature is not None else 0.7
        memory_status = "开启" if user_settings.memory_enabled else "关闭"
        return CommandResult(
            True,
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
            "记忆为空。" if not rows else "\n".join(f"{i + 1}. {row.content}" for i, row in enumerate(rows))
        )
        return CommandResult(True, listing)
    if action == "add" and len(parts) >= 3:
        db.add(Memory(user_id=user_id, content=parts[2][:2000]))
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
