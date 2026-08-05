"""固定的 0.1 初始 schema；迁移历史不得导入当前 ORM 模型。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    message_direction = postgresql.ENUM(
        "inbound", "outbound", name="messagedirection", create_type=False
    )
    message_status = postgresql.ENUM(
        "received", "queued", "processing", "sent", "failed", "ignored",
        name="messagestatus", create_type=False,
    )
    postgresql.ENUM("inbound", "outbound", name="messagedirection").create(
        op.get_bind(), checkfirst=True
    )
    postgresql.ENUM(
        "received", "queued", "processing", "sent", "failed", "ignored",
        name="messagestatus",
    ).create(op.get_bind(), checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("display_name", sa.String(255)),
        sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_table(
        "channel_identities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(40), nullable=False, server_default="wecom_kf"),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("external_id_hash", sa.String(64), nullable=False),
        sa.Column("external_id_encrypted", sa.Text(), nullable=False),
        sa.UniqueConstraint("channel", "account_id", "external_id_hash"),
    )
    op.create_index("ix_channel_identities_user_id", "channel_identities", ["user_id"])
    op.create_index("ix_channel_identities_external_id_hash", "channel_identities", ["external_id_hash"])
    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("provider", sa.String(80)),
        sa.Column("model", sa.String(160)),
        sa.Column("system_prompt", sa.Text()),
        sa.Column("temperature", sa.Float()),
        sa.Column("max_tokens", sa.Integer()),
        sa.Column("context_messages", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("memory_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("daily_token_quota", sa.Integer()),
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="SET NULL")),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("channel", sa.String(40), nullable=False, server_default="wecom_kf"),
        sa.Column("external_message_id", sa.String(255), nullable=False),
        sa.Column("direction", message_direction, nullable=False),
        sa.Column("message_type", sa.String(40), nullable=False),
        sa.Column("content", sa.Text()),
        sa.Column("status", message_status, nullable=False, server_default="received"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("channel", "external_message_id"),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_user_id", "messages", ["user_id"])
    op.create_table("channel_states", sa.Column("key", sa.String(255), primary_key=True), sa.Column("value", sa.Text(), nullable=False))
    op.create_table(
        "memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_memories_user_id", "memories", ["user_id"])
    op.create_table(
        "usage_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_usage_records_user_id", "usage_records", ["user_id"])
    op.create_index("ix_usage_records_created_at", "usage_records", ["created_at"])
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade():
    for table in [
        "audit_logs", "usage_records", "memories", "channel_states", "messages",
        "conversations", "user_settings", "channel_identities", "users",
    ]:
        op.drop_table(table)
    postgresql.ENUM(name="messagestatus").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="messagedirection").drop(op.get_bind(), checkfirst=True)
