"""平台模型供应商、模型组与故障切换路由。"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "platform_providers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("provider_key", sa.String(40), nullable=False, server_default="openai-compatible"),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_platform_providers_enabled", "platform_providers", ["enabled"])

    op.create_table(
        "model_groups",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_model_groups_is_default", "model_groups", ["is_default"])
    op.create_index("ix_model_groups_enabled", "model_groups", ["enabled"])
    op.create_index(
        "uq_model_groups_single_default",
        "model_groups",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.create_table(
        "model_routes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "group_id",
            sa.String(36),
            sa.ForeignKey("model_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_id",
            sa.String(36),
            sa.ForeignKey("platform_providers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "group_id", "provider_id", "model", name="uq_model_route_target"
        ),
    )
    op.create_index("ix_model_routes_group_id", "model_routes", ["group_id"])
    op.create_index("ix_model_routes_provider_id", "model_routes", ["provider_id"])
    op.create_index("ix_model_routes_enabled", "model_routes", ["enabled"])
    op.add_column("user_settings", sa.Column("model_group_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_user_settings_model_group_id",
        "user_settings",
        "model_groups",
        ["model_group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_user_settings_model_group_id", "user_settings", ["model_group_id"])


def downgrade():
    op.drop_index("ix_user_settings_model_group_id", table_name="user_settings")
    op.drop_constraint(
        "fk_user_settings_model_group_id", "user_settings", type_="foreignkey"
    )
    op.drop_column("user_settings", "model_group_id")
    op.drop_index("ix_model_routes_enabled", table_name="model_routes")
    op.drop_index("ix_model_routes_provider_id", table_name="model_routes")
    op.drop_index("ix_model_routes_group_id", table_name="model_routes")
    op.drop_table("model_routes")
    op.drop_index("uq_model_groups_single_default", table_name="model_groups")
    op.drop_index("ix_model_groups_enabled", table_name="model_groups")
    op.drop_index("ix_model_groups_is_default", table_name="model_groups")
    op.drop_table("model_groups")
    op.drop_index("ix_platform_providers_enabled", table_name="platform_providers")
    op.drop_table("platform_providers")
