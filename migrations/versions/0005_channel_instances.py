"""P1 多渠道基础：既有渠道实例的唯一约束与消息检索索引。"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    # channel_instances 在 0003 已建立；这里只补足管理 API 所需的名称唯一性。
    op.create_unique_constraint(
        "uq_channel_instance_name",
        "channel_instances",
        ["channel", "instance_name"],
    )
    op.create_index("ix_messages_channel", "messages", ["channel"])


def downgrade():
    op.drop_index("ix_messages_channel", table_name="messages")
    op.drop_constraint("uq_channel_instance_name", "channel_instances", type_="unique")
