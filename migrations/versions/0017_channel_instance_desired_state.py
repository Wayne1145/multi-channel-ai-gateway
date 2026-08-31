"""分离渠道实例控制意图与实时状态快照。"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "channel_instances",
        sa.Column("desired_running", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # 升级前所有非 offline 实例都由用户启动过，应继续自动恢复；offline 保持显式停止。
    op.execute("UPDATE channel_instances SET desired_running = true WHERE status <> 'offline'")


def downgrade():
    op.drop_column("channel_instances", "desired_running")