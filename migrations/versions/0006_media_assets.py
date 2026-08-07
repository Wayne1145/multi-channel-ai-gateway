"""P1 媒体安全生命周期：媒体元数据表。"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=True),
        sa.Column("channel", sa.String(40), nullable=False),
        sa.Column("media_type", sa.String(40), nullable=False),
        sa.Column("mime", sa.String(120), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="stored"),
        sa.Column("rejected_reason", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_media_assets_message_id", "media_assets", ["message_id"])
    op.create_index("ix_media_assets_channel", "media_assets", ["channel"])
    op.create_index("ix_media_assets_sha256", "media_assets", ["sha256"])
    op.create_index("ix_media_assets_expires_at", "media_assets", ["expires_at"])


def downgrade():
    op.drop_index("ix_media_assets_expires_at", table_name="media_assets")
    op.drop_index("ix_media_assets_sha256", table_name="media_assets")
    op.drop_index("ix_media_assets_channel", table_name="media_assets")
    op.drop_index("ix_media_assets_message_id", table_name="media_assets")
    op.drop_table("media_assets")
