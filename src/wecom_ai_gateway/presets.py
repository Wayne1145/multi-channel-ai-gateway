"""用户预设：一组配置快照，聊天指令一键切换。

快照内容：model / temperature / max_tokens / context_messages / memory_enabled
/ system_prompt / active_card_id。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CharacterCard, Preset, UserSettings

SNAPSHOT_KEYS = [
    "model",
    "temperature",
    "max_tokens",
    "context_messages",
    "memory_enabled",
    "system_prompt",
    "active_card_id",
]


def snapshot_settings(user_settings: UserSettings) -> dict:
    """采集当前用户设置的快照（只取受控字段）。"""
    return {key: getattr(user_settings, key) for key in SNAPSHOT_KEYS}


def apply_snapshot(db: Session, user_settings: UserSettings, config: dict) -> None:
    """把快照应用到用户设置（仅覆盖存在且属于当前用户的字段）。"""
    for key in SNAPSHOT_KEYS:
        if key in config:
            if key == "active_card_id" and config[key] is not None:
                card = db.scalar(
                    select(CharacterCard).where(
                        CharacterCard.id == config[key],
                        CharacterCard.user_id == user_settings.user_id,
                    )
                )
                if card is None:
                    continue
            setattr(user_settings, key, config[key])


def save_preset(db: Session, user_id: str, name: str, config: dict) -> Preset:
    row = db.scalar(
        select(Preset).where(Preset.user_id == user_id, Preset.name == name)
    )
    if row:
        row.config = config
    else:
        row = Preset(user_id=user_id, name=name, config=config)
        db.add(row)
    db.flush()
    return row


def list_presets(db: Session, user_id: str) -> list[Preset]:
    return list(
        db.scalars(
            select(Preset).where(Preset.user_id == user_id).order_by(Preset.created_at)
        )
    )


def get_preset(db: Session, user_id: str, name: str) -> Preset | None:
    return db.scalar(select(Preset).where(Preset.user_id == user_id, Preset.name == name))


def delete_preset(db: Session, user_id: str, name: str) -> bool:
    row = get_preset(db, user_id, name)
    if not row:
        return False
    db.delete(row)
    return True
