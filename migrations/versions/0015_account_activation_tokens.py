"""新增微信用户后台账号自助激活令牌。"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "account_activation_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
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
        "ix_account_activation_tokens_user_id",
        "account_activation_tokens",
        ["user_id"],
        unique=True,
    )
    op.create_index(
        "ix_account_activation_tokens_token_hash",
        "account_activation_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_account_activation_tokens_expires_at",
        "account_activation_tokens",
        ["expires_at"],
    )


def downgrade():
    op.drop_index("ix_account_activation_tokens_expires_at", table_name="account_activation_tokens")
    op.drop_index("ix_account_activation_tokens_token_hash", table_name="account_activation_tokens")
    op.drop_index("ix_account_activation_tokens_user_id", table_name="account_activation_tokens")
    op.drop_table("account_activation_tokens")