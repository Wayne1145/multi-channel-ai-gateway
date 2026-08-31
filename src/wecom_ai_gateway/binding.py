"""跨渠道统一身份：绑定码生成、校验与身份/数据合并。

用法：
- 用户在渠道 A 发送 `/bind`，得到高熵一次性绑定令牌（10 分钟有效）；
- 在渠道 B 发送 `/bind <码>`，B 渠道身份及名下数据合并到 A 用户；
- 合并后 B 用户删除，B 的登录账号与会话一并清除（B 渠道后续消息归属 A 用户）。

安全：绑定令牌至少 128 位熵（短时效 + 单次使用），只允许通过真实渠道消息换取。
"""

import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import (
    Account,
    AuthSession,
    BindCode,
    ChannelIdentity,
    ChannelInstance,
    CharacterCard,
    CommandPolicy,
    Conversation,
    KnowledgeChunk,
    KnowledgeItem,
    Memory,
    Message,
    MfaChallenge,
    MfaCredential,
    Preset,
    SettingOverride,
    UsageRecord,
    User,
    UserProvider,
    UserSettings,
)

log = logging.getLogger(__name__)

BIND_CODE_TTL_MINUTES = 10
BIND_CODE_LIFETIME = timedelta(minutes=BIND_CODE_TTL_MINUTES)


def _valid_bind_row(db: Session, code: str) -> BindCode:
    row = db.scalar(
        select(BindCode).where(BindCode.code == code.strip()).with_for_update()
    )
    if not row:
        raise ValueError("绑定码不存在，请检查后重试。")
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires < datetime.now(UTC):
        db.delete(row)
        db.commit()
        raise ValueError("绑定码已过期，请重新获取。")
    return row


def _merge_preview(db: Session, *, source_user_id: str, target_user_id: str) -> dict:
    """构造不含内部用户标识的合并预览。"""
    if source_user_id == target_user_id:
        raise ValueError("该绑定码属于当前账号，无需合并。")
    source_cards = set(
        db.scalars(select(CharacterCard.name).where(CharacterCard.user_id == source_user_id))
    )
    target_cards = set(
        db.scalars(select(CharacterCard.name).where(CharacterCard.user_id == target_user_id))
    )
    source_presets = set(
        db.scalars(select(Preset.name).where(Preset.user_id == source_user_id))
    )
    target_presets = set(
        db.scalars(select(Preset.name).where(Preset.user_id == target_user_id))
    )
    source_knowledge = set(
        db.scalars(select(KnowledgeItem.title).where(KnowledgeItem.user_id == source_user_id))
    )
    target_knowledge = set(
        db.scalars(select(KnowledgeItem.title).where(KnowledgeItem.user_id == target_user_id))
    )
    return {
        "counts": {
            "identities": db.query(ChannelIdentity).filter_by(user_id=source_user_id).count(),
            "messages": db.query(Message).filter_by(user_id=source_user_id).count(),
            "memories": db.query(Memory).filter_by(user_id=source_user_id).count(),
            "cards": len(source_cards),
            "presets": len(source_presets),
            "knowledge_items": len(source_knowledge),
            "channel_instances": db.query(ChannelInstance)
            .filter_by(owner_user_id=source_user_id)
            .count(),
        },
        "conflicts": {
            "card_names": sorted(source_cards & target_cards),
            "preset_names": sorted(source_presets & target_presets),
            "knowledge_titles": sorted(source_knowledge & target_knowledge),
            # 合并策略是保留当前账号设置；明确展示，避免静默丢弃源配置。
            "user_settings": db.get(UserSettings, source_user_id) is not None,
        },
    }


def merge_preview(db: Session, code: str, *, target_user_id: str) -> dict:
    """仅预览 code 所属源用户并入当前登录用户的影响，不修改任何数据。"""
    row = _valid_bind_row(db, code)
    if row.user_id == target_user_id:
        raise ValueError("该绑定码属于当前账号，无需合并。")
    return _merge_preview(db, source_user_id=row.user_id, target_user_id=target_user_id)


def merge_by_code(db: Session, code: str, *, target_user_id: str) -> dict:
    """将绑定码所属源用户并入当前登录用户；当前后台账号始终保留。"""
    row = _valid_bind_row(db, code)
    if row.user_id == target_user_id:
        raise ValueError("该绑定码属于当前账号，无需合并。")
    preview = _merge_preview(db, source_user_id=row.user_id, target_user_id=target_user_id)
    stats = _merge_into(
        db,
        source_user_id=row.user_id,
        target_user_id=target_user_id,
    )
    db.delete(row)
    db.commit()
    return {"ok": True, "stats": stats, "preview": preview}


def create_bind_code(db: Session, user_id: str) -> str:
    """为用户生成一次性绑定码；同用户旧码作废。"""
    db.execute(delete(BindCode).where(BindCode.user_id == user_id))
    while True:
        # 32 位十六进制令牌提供 128 位熵，无法在有效期内枚举。
        code = secrets.token_hex(16)
        if not db.scalar(select(BindCode).where(BindCode.code == code)):
            break
    db.add(
        BindCode(
            code=code,
            user_id=user_id,
            expires_at=datetime.now(UTC) + BIND_CODE_LIFETIME,
        )
    )
    db.commit()
    return code


def resolve_bind(
    db: Session,
    code: str,
    *,
    user_id: str,
    channel: str,
    account_id: str,
    external_id: str,
) -> dict:
    """校验绑定码并合并当前渠道身份到码所属用户。"""
    row = db.scalar(select(BindCode).where(BindCode.code == code.strip()))
    if not row:
        return {"ok": False, "message": "绑定码不存在，请检查后重试。"}
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)  # SQLite 读出为 naive，按 UTC 处理
    if expires < datetime.now(UTC):
        db.delete(row)
        db.commit()
        return {"ok": False, "message": "绑定码已过期，请重新发送 /bind 获取。"}
    target_user_id = row.user_id
    if target_user_id == user_id:
        db.delete(row)
        db.commit()
        return {"ok": False, "message": "该绑定码属于当前账号，无需合并。"}

    # 当前渠道身份（按 user_id+channel+account 定位）
    identity = db.scalar(
        select(ChannelIdentity).where(
            ChannelIdentity.user_id == user_id,
            ChannelIdentity.channel == channel,
            ChannelIdentity.account_id == account_id,
        )
    )
    if identity is None:
        return {"ok": False, "message": "无法识别当前渠道身份。"}

    stats = _merge_into(db, source_user_id=user_id, target_user_id=target_user_id)
    db.delete(row)
    db.commit()
    return {
        "ok": True,
        "message": (
            f"绑定成功：当前渠道已并入目标账号。"
            f"迁移身份 {stats['identities']} 个、消息 {stats['messages']} 条、"
            f"记忆 {stats['memories']} 条、角色卡 {stats['cards']} 张、预设 {stats['presets']} 个。"
        ),
    }


def _merge_into(db: Session, *, source_user_id: str, target_user_id: str) -> dict:
    """把 source 用户的数据迁移到 target，然后删除 source 用户。"""
    stats = {"identities": 0, "messages": 0, "memories": 0, "cards": 0, "presets": 0}

    for identity in db.scalars(
        select(ChannelIdentity).where(ChannelIdentity.user_id == source_user_id)
    ):
        identity.user_id = target_user_id
        stats["identities"] += 1

    for model, stat_key in [
        (Message, "messages"),
        (Conversation, "conversations"),
        (Memory, "memories"),
    ]:
        count = db.query(model).filter(model.user_id == source_user_id).update(
            {model.user_id: target_user_id}
        )
        if stat_key == "messages":
            stats[stat_key] = count

    # 同名角色卡/预设保留 target 的，跳过 source 的同名项避免唯一约束冲突
    target_card_names = set(
        db.scalars(select(CharacterCard.name).where(CharacterCard.user_id == target_user_id))
    )
    for card in db.scalars(
        select(CharacterCard).where(CharacterCard.user_id == source_user_id)
    ):
        if card.name in target_card_names:
            db.delete(card)
        else:
            card.user_id = target_user_id
            stats["cards"] += 1
    target_preset_names = set(
        db.scalars(select(Preset.name).where(Preset.user_id == target_user_id))
    )
    for preset in db.scalars(
        select(Preset).where(Preset.user_id == source_user_id)
    ):
        if preset.name in target_preset_names:
            db.delete(preset)
        else:
            preset.user_id = target_user_id
            stats["presets"] += 1

    # BYOK 直接迁移（无同名约束）
    db.query(UserProvider).filter(UserProvider.user_id == source_user_id).update(
        {UserProvider.user_id: target_user_id}
    )
    # 渠道实例所有权必须随身份迁移，否则删除源用户后实例会因 SET NULL 变成无主。
    db.query(ChannelInstance).filter(ChannelInstance.owner_user_id == source_user_id).update(
        {ChannelInstance.owner_user_id: target_user_id}
    )
    # 历史用量必须保留；它用于配额和审计，不能随源用户级联删除。
    db.query(UsageRecord).filter(UsageRecord.user_id == source_user_id).update(
        {UsageRecord.user_id: target_user_id}
    )

    # 用户级策略/覆盖项按唯一键合并：目标已有配置优先，源冲突项删除。
    target_policy_keys = set(
        db.execute(
            select(CommandPolicy.channel, CommandPolicy.command).where(
                CommandPolicy.user_id == target_user_id
            )
        )
    )
    for policy in db.scalars(
        select(CommandPolicy).where(CommandPolicy.user_id == source_user_id)
    ):
        if (policy.channel, policy.command) in target_policy_keys:
            db.delete(policy)
        else:
            policy.user_id = target_user_id
    target_override_keys = set(
        db.execute(
            select(SettingOverride.key).where(
                SettingOverride.scope_type == "user",
                SettingOverride.scope_id == target_user_id,
            )
        ).scalars()
    )
    for override in db.scalars(
        select(SettingOverride).where(
            SettingOverride.scope_type == "user",
            SettingOverride.scope_id == source_user_id,
        )
    ):
        if override.key in target_override_keys:
            db.delete(override)
        else:
            override.scope_id = target_user_id
    target_knowledge_titles = set(
        db.scalars(select(KnowledgeItem.title).where(KnowledgeItem.user_id == target_user_id))
    )
    for item in db.scalars(
        select(KnowledgeItem).where(KnowledgeItem.user_id == source_user_id)
    ):
        if item.title in target_knowledge_titles:
            db.delete(item)
        else:
            item.user_id = target_user_id
            db.query(KnowledgeChunk).filter(KnowledgeChunk.item_id == item.id).update(
                {KnowledgeChunk.user_id: target_user_id}
            )

    # 清理 source 账号与会话
    source_account_ids = list(
        db.scalars(select(Account.id).where(Account.user_id == source_user_id))
    )
    db.execute(delete(AuthSession).where(AuthSession.user_id == source_user_id))
    db.execute(delete(MfaChallenge).where(MfaChallenge.user_id == source_user_id))
    if source_account_ids:
        # MfaCredential 使用通用 subject_id，数据库无法声明账号外键，必须显式清理。
        db.execute(
            delete(MfaCredential).where(
                MfaCredential.subject_type == "account",
                MfaCredential.subject_id.in_(source_account_ids),
            )
        )
    db.execute(delete(Account).where(Account.user_id == source_user_id))
    source = db.get(User, source_user_id)
    if source:
        db.delete(source)
    return stats
