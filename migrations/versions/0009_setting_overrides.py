"""渠道/用户级设置覆盖表。"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "setting_overrides",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(128), nullable=False),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "scope_type", "scope_id", "key", name="uq_setting_overrides_scope_key"
        ),
    )
    op.create_index("ix_setting_overrides_scope", "setting_overrides", ["scope_type", "scope_id"])


def downgrade():
    op.drop_index("ix_setting_overrides_scope", table_name="setting_overrides")
    op.drop_table("setting_overrides")
