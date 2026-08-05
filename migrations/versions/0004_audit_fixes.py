"""审计修复：为旧实例补充任务租约字段。

策略作用域唯一索引在 0003 已随新安装创建；避免为已升级实例重复建索引。
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("outbox_tasks", sa.Column("lease_token", sa.String(36), nullable=True))
    op.create_index("ix_outbox_tasks_lease_token", "outbox_tasks", ["lease_token"])


def downgrade():
    op.drop_index("ix_outbox_tasks_lease_token", table_name="outbox_tasks")
    op.drop_column("outbox_tasks", "lease_token")
