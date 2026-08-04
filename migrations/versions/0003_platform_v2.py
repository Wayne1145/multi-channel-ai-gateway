"""P1：角色卡、BYOK、指令策略、渠道实例、预设、平台配置与用户模式扩展。

- 新表：platform_config / character_cards / user_providers / command_policies
        / channel_instances / presets
- users + mode（每用户模式覆盖）
- user_settings + active_card_id / provider_key
- memories + content_encrypted（新写入的记忆加密存储）
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _is_pg() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # 平台级 key-value 配置
    op.create_table(
        "platform_config",
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )

    # 角色卡（内容加密存储）
    op.create_table(
        "character_cards",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("format", sa.String(length=20), server_default="soul_md", nullable=False),
        sa.Column("content_encrypted", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_character_cards_user_id", "character_cards", ["user_id"])

    # 用户自带供应商（BYOK，密钥加密存储）
    op.create_table(
        "user_providers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("provider_key", sa.String(length=40), server_default="openai-compatible", nullable=False),
        sa.Column("base_url", sa.String(length=255), nullable=True),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("models", sa.JSON(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_user_providers_user_id", "user_providers", ["user_id"])

    # 指令策略（平台/渠道/用户 三级，user_id/channel 可空）
    op.create_table(
        "command_policies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("channel", sa.String(length=40), nullable=True),
        sa.Column("command", sa.String(length=60), nullable=False),
        sa.Column("allowed", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("silent_block", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "blocked_strategy",
            sa.String(length=20),
            server_default="redirect_to_ai",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "channel", "command"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_command_policies_user_id", "command_policies", ["user_id"])

    # 渠道实例（ClawBot 等多实例）
    op.create_table(
        "channel_instances",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("channel", sa.String(length=40), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=True),
        sa.Column("instance_name", sa.String(length=120), nullable=False),
        sa.Column("login_state", sa.JSON(), nullable=False),
        sa.Column("session_encrypted", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="offline", nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_channel_instances_channel", "channel_instances", ["channel"])
    op.create_index("ix_channel_instances_owner_user_id", "channel_instances", ["owner_user_id"])

    # 预设（配置快照）
    op.create_table(
        "presets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_presets_user_id", "presets", ["user_id"])

    # 扩展既有表
    op.add_column("users", sa.Column("mode", sa.String(length=20), nullable=True))
    op.add_column("user_settings", sa.Column("active_card_id", sa.String(length=36), nullable=True))
    op.add_column("user_settings", sa.Column("provider_key", sa.String(length=120), nullable=True))
    op.add_column("memories", sa.Column("content_encrypted", sa.Text(), nullable=True))
    if _is_pg():
        op.create_foreign_key(
            "fk_user_settings_active_card",
            "user_settings",
            "character_cards",
            ["active_card_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    if _is_pg():
        op.drop_constraint("fk_user_settings_active_card", "user_settings", type_="foreignkey")
    op.drop_column("memories", "content_encrypted")
    op.drop_column("user_settings", "provider_key")
    op.drop_column("user_settings", "active_card_id")
    op.drop_column("users", "mode")

    op.drop_index("ix_presets_user_id", table_name="presets")
    op.drop_table("presets")
    op.drop_index("ix_channel_instances_owner_user_id", table_name="channel_instances")
    op.drop_index("ix_channel_instances_channel", table_name="channel_instances")
    op.drop_table("channel_instances")
    op.drop_index("ix_command_policies_user_id", table_name="command_policies")
    op.drop_table("command_policies")
    op.drop_index("ix_user_providers_user_id", table_name="user_providers")
    op.drop_table("user_providers")
    op.drop_index("ix_character_cards_user_id", table_name="character_cards")
    op.drop_table("character_cards")
    op.drop_table("platform_config")
