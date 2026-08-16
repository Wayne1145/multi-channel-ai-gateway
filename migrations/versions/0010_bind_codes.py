"""跨渠道身份绑定码表。"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bind_codes",
        sa.Column("code", sa.String(8), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_bind_codes_user_id", "bind_codes", ["user_id"])


def downgrade():
    op.drop_index("ix_bind_codes_user_id", table_name="bind_codes")
    op.drop_table("bind_codes")
