"""持久化任务 Outbox 与死信状态。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("outbox_tasks"):
        outbox_status = postgresql.ENUM(
            "pending", "processing", "done", "dead", name="outboxstatus", create_type=False
        )
        postgresql.ENUM(
            "pending", "processing", "done", "dead", name="outboxstatus"
        ).create(bind, checkfirst=True)
        op.create_table(
            "outbox_tasks",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("task_type", sa.String(length=40), nullable=False),
            sa.Column("dedupe_key", sa.String(length=255), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("status", outbox_status, nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("rerun_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column(
                "available_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("dedupe_key"),
        )
        op.create_index("ix_outbox_tasks_task_type", "outbox_tasks", ["task_type"])
        op.create_index("ix_outbox_tasks_status", "outbox_tasks", ["status"])
        op.create_index("ix_outbox_tasks_available_at", "outbox_tasks", ["available_at"])
    # 老的 0.1.0 数据库枚举中没有 dead；全新库则由当前模型提前带入。
    op.execute("ALTER TYPE messagestatus ADD VALUE IF NOT EXISTS 'dead'")


def downgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table("outbox_tasks"):
        op.drop_index("ix_outbox_tasks_available_at", table_name="outbox_tasks", if_exists=True)
        op.drop_index("ix_outbox_tasks_status", table_name="outbox_tasks", if_exists=True)
        op.drop_index("ix_outbox_tasks_task_type", table_name="outbox_tasks", if_exists=True)
        op.drop_table("outbox_tasks")
    postgresql.ENUM(name="outboxstatus").drop(bind, checkfirst=True)
    # PostgreSQL 无法安全地原地删除枚举值；保留 messagestatus.dead。
