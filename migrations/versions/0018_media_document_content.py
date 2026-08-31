"""为受信渠道文档保存加密提取文本，不保存原始文件。"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("media_assets", sa.Column("content_encrypted", sa.Text(), nullable=True))
    op.add_column(
        "media_assets",
        sa.Column("content_chars", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_column("media_assets", "content_chars")
    op.drop_column("media_assets", "content_encrypted")