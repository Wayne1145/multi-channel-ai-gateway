"""扩展身份绑定令牌长度，支持 128 位高熵令牌。"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    # 旧短码立即失效，避免升级后仍可被枚举；用户可重新签发高熵令牌。
    op.execute("DELETE FROM bind_codes")
    op.alter_column(
        "bind_codes",
        "code",
        existing_type=sa.String(8),
        type_=sa.String(64),
        existing_nullable=False,
    )


def downgrade():
    op.execute("DELETE FROM bind_codes")
    op.alter_column(
        "bind_codes",
        "code",
        existing_type=sa.String(64),
        type_=sa.String(8),
        existing_nullable=False,
    )