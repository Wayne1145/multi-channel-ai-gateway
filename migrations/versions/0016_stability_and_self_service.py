"""渠道稳定性、自助找回、身份中心与知识库升级。"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "channel_identities",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column(
        "channel_identities",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_channel_identities_last_seen_at",
        "channel_identities",
        ["last_seen_at"],
    )
    op.add_column(
        "channel_instances",
        sa.Column("status_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "channel_instances",
        sa.Column("status_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "channel_instances",
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "channel_instances",
        sa.Column("last_online_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "channel_instances",
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "channel_instances",
        sa.Column("last_error", sa.String(120), nullable=True),
    )
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "account_id",
            sa.String(36),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_password_reset_tokens_user_id",
        "password_reset_tokens",
        ["user_id"],
        unique=True,
    )
    op.create_index(
        "ix_password_reset_tokens_account_id",
        "password_reset_tokens",
        ["account_id"],
    )
    op.create_index(
        "ix_password_reset_tokens_token_hash",
        "password_reset_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_password_reset_tokens_expires_at",
        "password_reset_tokens",
        ["expires_at"],
    )
    # pgvector 必须由数据库管理员或部署镜像预先安装；应用迁移不隐式要求 CREATE EXTENSION 权限。
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        installed = bind.execute(
            sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
        if not installed:
            raise RuntimeError(
                "PostgreSQL 缺少 pgvector 扩展；请由数据库管理员先执行 CREATE EXTENSION vector"
            )
    # 0011 尚未声明外键；历史孤儿条目无法安全归属，升级前显式清理，避免外键创建失败。
    op.execute(
        "DELETE FROM knowledge_items WHERE NOT EXISTS "
        "(SELECT 1 FROM users WHERE users.id = knowledge_items.user_id)"
    )
    op.add_column("knowledge_items", sa.Column("content_encrypted", sa.Text(), nullable=True))
    op.add_column("knowledge_items", sa.Column("content_sha256", sa.String(64), nullable=True))
    op.add_column(
        "knowledge_items",
        sa.Column("source_type", sa.String(30), nullable=False, server_default="text"),
    )
    op.add_column("knowledge_items", sa.Column("source_name", sa.String(255), nullable=True))
    op.add_column("knowledge_items", sa.Column("source_url", sa.String(1000), nullable=True))
    op.add_column("knowledge_items", sa.Column("mime_type", sa.String(160), nullable=True))
    op.add_column(
        "knowledge_items",
        sa.Column("content_chars", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("knowledge_items", sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_knowledge_items_user_id",
        "knowledge_items",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_knowledge_items_content_sha256", "knowledge_items", ["content_sha256"])
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "item_id",
            sa.String(36),
            sa.ForeignKey("knowledge_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content_encrypted", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("embedding", Vector(256), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("item_id", "chunk_index", name="uq_knowledge_chunk_index"),
    )

    op.create_index("ix_knowledge_chunks_item_id", "knowledge_chunks", ["item_id"])
    op.create_index("ix_knowledge_chunks_user_id", "knowledge_chunks", ["user_id"])
    op.create_index(
        "ix_knowledge_chunks_content_sha256", "knowledge_chunks", ["content_sha256"]
    )


def downgrade():
    op.drop_index("ix_knowledge_chunks_content_sha256", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_user_id", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_item_id", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_index("ix_knowledge_items_content_sha256", table_name="knowledge_items")
    op.drop_constraint("fk_knowledge_items_user_id", "knowledge_items", type_="foreignkey")
    op.drop_column("knowledge_items", "indexed_at")
    op.drop_column("knowledge_items", "content_chars")
    op.drop_column("knowledge_items", "mime_type")
    op.drop_column("knowledge_items", "source_url")
    op.drop_column("knowledge_items", "source_name")
    op.drop_column("knowledge_items", "source_type")
    op.drop_column("knowledge_items", "content_sha256")
    op.drop_column("knowledge_items", "content_encrypted")
    op.drop_index("ix_password_reset_tokens_expires_at", table_name="password_reset_tokens")
    op.drop_index("ix_password_reset_tokens_token_hash", table_name="password_reset_tokens")
    op.drop_index("ix_password_reset_tokens_account_id", table_name="password_reset_tokens")
    op.drop_index("ix_password_reset_tokens_user_id", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_column("channel_instances", "last_error")
    op.drop_column("channel_instances", "last_error_at")
    op.drop_column("channel_instances", "last_online_at")
    op.drop_column("channel_instances", "last_checked_at")
    op.drop_column("channel_instances", "status_updated_at")
    op.drop_column("channel_instances", "status_revision")
    op.drop_index("ix_channel_identities_last_seen_at", table_name="channel_identities")
    op.drop_column("channel_identities", "last_seen_at")
    op.drop_column("channel_identities", "created_at")