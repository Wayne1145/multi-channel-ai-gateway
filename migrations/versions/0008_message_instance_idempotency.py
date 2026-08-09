"""将入站消息幂等范围扩展到具体渠道实例。"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "messages",
        sa.Column("channel_instance_id", sa.String(128), nullable=False, server_default=""),
    )
    op.execute(
        """
        UPDATE messages
        SET channel_instance_id = CASE
            WHEN channel = 'wecom_kf' THEN COALESCE(metadata_json->>'open_kfid', '')
            ELSE COALESCE(metadata_json->>'instance_id', '')
        END
        """
    )
    op.drop_constraint("messages_channel_external_message_id_key", "messages", type_="unique")
    op.create_unique_constraint(
        "uq_messages_channel_instance_external",
        "messages",
        ["channel", "channel_instance_id", "external_message_id"],
    )


def downgrade():
    op.drop_constraint("uq_messages_channel_instance_external", "messages", type_="unique")
    op.create_unique_constraint(
        "messages_channel_external_message_id_key",
        "messages",
        ["channel", "external_message_id"],
    )
    op.drop_column("messages", "channel_instance_id")