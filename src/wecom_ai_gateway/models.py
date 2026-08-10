import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    literal_column,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class MessageDirection(str, enum.Enum):
    inbound = "inbound"
    outbound = "outbound"


class MessageStatus(str, enum.Enum):
    received = "received"
    queued = "queued"
    processing = "processing"
    sent = "sent"
    failed = "failed"
    ignored = "ignored"
    dead = "dead"


class OutboxStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    done = "done"
    dead = "dead"


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    display_name: Mapped[str | None] = mapped_column(String(255))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    # self_service | managed | NULL=跟随平台默认（single 只作全局开关，不做用户级）
    mode: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Account(Base):
    """后台登录账号；聊天用户与登录凭据分离。"""

    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthSession(Base):
    """不透明后台会话；数据库只保存令牌摘要。"""

    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(20))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChannelIdentity(Base):
    __tablename__ = "channel_identities"
    __table_args__ = (UniqueConstraint("channel", "account_id", "external_id_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(40), default="wecom_kf")
    account_id: Mapped[str] = mapped_column(String(128))
    external_id_hash: Mapped[str] = mapped_column(String(64), index=True)
    external_id_encrypted: Mapped[str] = mapped_column(Text)
    user: Mapped[User] = relationship()


class UserSettings(Base):
    __tablename__ = "user_settings"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    provider: Mapped[str | None] = mapped_column(String(80))
    model: Mapped[str | None] = mapped_column(String(160))
    system_prompt: Mapped[str | None] = mapped_column(Text)
    temperature: Mapped[float | None] = mapped_column(Float)
    max_tokens: Mapped[int | None] = mapped_column(Integer)
    context_messages: Mapped[int] = mapped_column(Integer, default=20)
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_token_quota: Mapped[int | None] = mapped_column(Integer)
    # 当前生效角色卡（NULL=无卡，使用纯人设）
    active_card_id: Mapped[str | None] = mapped_column(
        ForeignKey("character_cards.id", ondelete="SET NULL")
    )
    # 当前生效供应商：NULL/空=平台默认，byok:<provider_id>=用户自带
    provider_key: Mapped[str | None] = mapped_column(String(120))


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("channel", "channel_instance_id", "external_message_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), index=True
    )
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    channel: Mapped[str] = mapped_column(String(40), default="wecom_kf", index=True)
    channel_instance_id: Mapped[str] = mapped_column(String(128), default="")
    external_message_id: Mapped[str] = mapped_column(String(255))
    direction: Mapped[MessageDirection] = mapped_column(Enum(MessageDirection))
    message_type: Mapped[str] = mapped_column(String(40))
    content: Mapped[str | None] = mapped_column(Text)
    status: Mapped[MessageStatus] = mapped_column(Enum(MessageStatus), default=MessageStatus.received)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChannelState(Base):
    __tablename__ = "channel_states"
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Memory(Base):
    __tablename__ = "memories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    # 0.3.0 起新写入的记忆加密存储；content 保留旧明文（如存在）仅供回退读取
    content_encrypted: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsageRecord(Base):
    __tablename__ = "usage_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(160))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(120))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboxTask(Base):
    __tablename__ = "outbox_tasks"
    __table_args__ = (UniqueConstraint("dedupe_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_type: Mapped[str] = mapped_column(String(40), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(255))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus), default=OutboxStatus.pending, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    rerun_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 每次领取都会更换租约令牌；旧 Worker 无权完成或回退新租约。
    lease_token: Mapped[str | None] = mapped_column(String(36), index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PlatformConfig(Base):
    """平台级 key-value 配置（模式、默认策略等）。"""

    __tablename__ = "platform_config"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SettingOverride(Base):
    """渠道/用户级设置覆盖；解析优先级：用户 > 渠道 > 平台配置 > .env。"""

    __tablename__ = "setting_overrides"
    __table_args__ = (
        UniqueConstraint(
            "scope_type", "scope_id", "key", name="uq_setting_overrides_scope_key"
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    scope_type: Mapped[str] = mapped_column(String(16), index=True)  # user | channel
    scope_id: Mapped[str] = mapped_column(String(128))
    key: Mapped[str] = mapped_column(String(120))
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CharacterCard(Base):
    """用户角色卡槽位。content_encrypted 入库即密文，管理员侧不可读。"""

    __tablename__ = "character_cards"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    format: Mapped[str] = mapped_column(String(20), default="soul_md")  # soul_md | st_v2 | st_v3
    content_encrypted: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserProvider(Base):
    """用户自带模型供应商（BYOK）。api_key_encrypted 入库即密文。"""

    __tablename__ = "user_providers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider_key: Mapped[str] = mapped_column(String(40), default="openai-compatible")
    base_url: Mapped[str | None] = mapped_column(String(255))
    api_key_encrypted: Mapped[str] = mapped_column(Text)
    models: Mapped[list] = mapped_column(JSON, default=list)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CommandPolicy(Base):
    """指令策略：平台默认(全空) → 渠道(channel) → 用户(user_id) 三级覆盖。"""

    __tablename__ = "command_policies"
    __table_args__ = (
        Index(
            "uq_command_policy_scope",
            func.coalesce(literal_column("user_id"), ""),
            func.coalesce(literal_column("channel"), ""),
            literal_column("command"),
            unique=True,
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str | None] = mapped_column(String(40))  # NULL=全部渠道
    command: Mapped[str] = mapped_column(String(60))  # 小写、无前导斜杠
    allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    silent_block: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_strategy: Mapped[str] = mapped_column(String(20), default="redirect_to_ai")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ChannelInstance(Base):
    """渠道实例：企微客服账号 / 一个扫码绑定的 ClawBot 微信 / 未来飞书应用等。"""

    __tablename__ = "channel_instances"
    __table_args__ = (UniqueConstraint("channel", "instance_name", name="uq_channel_instance_name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel: Mapped[str] = mapped_column(String(40), index=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    instance_name: Mapped[str] = mapped_column(String(120))
    login_state: Mapped[dict] = mapped_column(JSON, default=dict)
    session_encrypted: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="offline")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Preset(Base):
    """用户预设：一组配置快照（模型+参数+人设+角色卡），指令一键切换。"""

    __tablename__ = "presets"
    __table_args__ = (UniqueConstraint("user_id", "name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MediaAsset(Base):
    """渠道媒体消息的安全元数据记录。

    只保存媒体元数据与内容哈希，不主动从外部 URL 拉取/落盘原始文件
    （避免 SSRF 与存储膨胀）；expires_at 到期后由 Worker 定时清理。
    """

    __tablename__ = "media_assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(40), index=True)
    media_type: Mapped[str] = mapped_column(String(40))  # image | voice | file
    mime: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    filename: Mapped[str | None] = mapped_column(String(255))
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    # 原始媒体定位（渠道侧 id/URL）。URL 可能含渠道凭据，管理端 API 一律不返回。
    storage_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="stored")  # stored | rejected | expired
    rejected_reason: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
