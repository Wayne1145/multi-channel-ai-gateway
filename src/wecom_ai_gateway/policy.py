"""双管理模式与指令策略解析。

模式解析优先级（由高到低）：
  1. settings.single_user_mode=True → "single"（全局单用户开关，不做用户级覆盖）
  2. users.mode（每用户覆盖：self_service | managed）
  3. platform_config["mode"]（平台级配置）
  4. settings.platform_mode（.env 默认）

指令策略三级覆盖（后层覆盖前层）：
  平台默认 (user_id=NULL, channel=NULL)
  → 渠道     (user_id=NULL, channel=X)
  → 用户     (user_id=U, channel=NULL 或 X)

静默禁用语义：
  - allowed=False, silent_block=False  → 回复"该指令不可用"
  - allowed=False, silent_block=True, blocked_strategy=redirect_to_ai → 当作普通消息交给 AI
  - allowed=False, silent_block=True, blocked_strategy=ignore → 无任何回复
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import CommandPolicy, PlatformConfig, User


def resolve_user_mode(db: Session, user: User | None) -> str:
    """返回用户当前生效的运行模式：single | self_service | managed。"""
    if settings.single_user_mode:
        return "single"
    if user and user.mode:
        return user.mode
    row = db.get(PlatformConfig, "mode")
    if row and row.value and row.value.get("mode") in {"self_service", "managed"}:
        return row.value["mode"]
    return settings.platform_mode


def normalize_command(text: str) -> str | None:
    """把 '/Card Use x' 归一化为 'card'；非命令返回 None。"""
    if not text.startswith("/"):
        return None
    parts = text.strip().split()
    if not parts:
        return None
    return parts[0].lstrip("/").lower()


@dataclass
class CommandDecision:
    allowed: bool
    silent_block: bool = False
    blocked_strategy: str = "redirect_to_ai"  # redirect_to_ai | ignore
    source: str = "default"  # default | platform | channel | user


def get_command_decision(
    db: Session, user_id: str, channel: str, command: str
) -> CommandDecision:
    """解析某用户在某渠道对某指令的最终策略；无任何策略行时默认放行。"""
    rows = list(
        db.scalars(
            select(CommandPolicy)
            .where(CommandPolicy.command == command)
            .order_by(CommandPolicy.created_at.desc())
        )
    )
    best = None
    best_score = -1
    for row in rows:
        # 跳过不适用于该用户/渠道的行
        if row.user_id is not None and row.user_id != user_id:
            continue
        if row.channel is not None and row.channel != channel:
            continue
        # 越具体优先级越高：命中用户 +2，命中渠道 +1
        score = (2 if row.user_id == user_id else 0) + (1 if row.channel == channel else 0)
        if score > best_score:
            best = row
            best_score = score
    if best is None:
        return CommandDecision(allowed=True, source="default")
    source = "user" if best.user_id == user_id else ("channel" if best.channel == channel else "platform")
    return CommandDecision(
        allowed=best.allowed,
        silent_block=best.silent_block,
        blocked_strategy=best.blocked_strategy,
        source=source,
    )
