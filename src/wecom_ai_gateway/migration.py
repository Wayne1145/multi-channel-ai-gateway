"""模式切换迁移服务。

self_service（用户自足）↔ managed（统一管理）切换本身不复制数据：
所有角色卡/预设/记忆/BYOK 从创建起就按 user_id 隔离并加密存储，
管理员侧始终只读元数据。迁移函数负责：

- 校验目标模式合法性；
- 写入 AuditLog 审计轨迹；
- 返回切换摘要（用户数据规模），便于管理端确认迁移边界。

语义边界（roadmap v2 §3.1）：
- managed → self_service：用户数据本来就私有，无需复制；管理员分发
  的人设若存在（系统提示等）会继续保留，由用户自行决定是否保留；
- self_service → managed：管理员获得策略控制权，但不得读取用户内容。
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    AuditLog,
    CharacterCard,
    Memory,
    Preset,
    UsageRecord,
    User,
    UserProvider,
)

VALID_MODES = {"self_service", "managed", None}


def migrate_user_mode(db: Session, user: User, target_mode: str | None) -> dict:
    """把用户切换到目标模式并写审计日志，返回用户数据规模摘要。"""
    if target_mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}")
    old_mode = user.mode
    user.mode = target_mode
    summary = _user_scale(db, user.id)
    db.add(
        AuditLog(
            user_id=user.id,
            action="mode.migrate",
            detail={"from": old_mode, "to": target_mode, "scale": summary},
        )
    )
    return {"from": old_mode, "to": target_mode, "scale": summary}


def _user_scale(db: Session, user_id: str) -> dict:
    cards = db.scalar(
        select(func.count()).select_from(CharacterCard).where(CharacterCard.user_id == user_id)
    )
    presets = db.scalar(
        select(func.count()).select_from(Preset).where(Preset.user_id == user_id)
    )
    memories = db.scalar(
        select(func.count()).select_from(Memory).where(Memory.user_id == user_id)
    )
    providers = db.scalar(
        select(func.count()).select_from(UserProvider).where(UserProvider.user_id == user_id)
    )
    tokens = db.scalar(
        select(func.coalesce(func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens), 0))
        .where(UsageRecord.user_id == user_id)
    )
    return {
        "cards": cards or 0,
        "presets": presets or 0,
        "memories": memories or 0,
        "providers": providers or 0,
        "total_tokens": int(tokens or 0),
    }
