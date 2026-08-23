"""0014 新增用户设置字段 command_guidance_enabled 与渠道身份绑定状态。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    # 1) 命令指引开关：用户可在 Web 控制台角色卡设置页开关"高级命令指引"。
    #    关闭时系统提示词中不再注入 /help 等命令索引，改善角色扮演沉浸度；
    #    记忆库、网络搜索、工具调用提示词不受影响。
    op.add_column(
        "user_settings",
        sa.Column("command_guidance_enabled", sa.Boolean(), nullable=True, default=True),
    )
    # 存量用户默认开启，保持现有体验
    op.execute(
        "UPDATE user_settings SET command_guidance_enabled = TRUE WHERE command_guidance_enabled IS NULL"
    )

    # 2) ChannelIdentity 增加 bind_state 字段，记录绑定来源（manual / qr / auto）。
    op.add_column(
        "channel_identities",
        sa.Column("bind_state", sa.JSON(), nullable=True, default=dict),
    )


def downgrade():
    op.drop_column("channel_identities", "bind_state")
    op.drop_column("user_settings", "command_guidance_enabled")