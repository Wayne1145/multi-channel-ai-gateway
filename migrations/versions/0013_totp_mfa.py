"""TOTP 多因素认证凭据与短时登录挑战。"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mfa_credentials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("subject_type", sa.String(20), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("recovery_code_hashes", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("last_totp_counter", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("subject_type", "subject_id", name="uq_mfa_credential_subject"),
    )
    op.create_index("ix_mfa_credentials_subject_type", "mfa_credentials", ["subject_type"])
    op.create_index("ix_mfa_credentials_subject_id", "mfa_credentials", ["subject_id"])
    op.create_index("ix_mfa_credentials_enabled", "mfa_credentials", ["enabled"])
    op.create_table(
        "mfa_challenges",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("subject_type", sa.String(20), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id", ondelete="CASCADE")),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_mfa_challenges_token_hash", "mfa_challenges", ["token_hash"])
    op.create_index("ix_mfa_challenges_subject_type", "mfa_challenges", ["subject_type"])
    op.create_index("ix_mfa_challenges_subject_id", "mfa_challenges", ["subject_id"])
    op.create_index("ix_mfa_challenges_account_id", "mfa_challenges", ["account_id"])
    op.create_index("ix_mfa_challenges_user_id", "mfa_challenges", ["user_id"])
    op.create_index("ix_mfa_challenges_expires_at", "mfa_challenges", ["expires_at"])


def downgrade():
    op.drop_table("mfa_challenges")
    op.drop_table("mfa_credentials")