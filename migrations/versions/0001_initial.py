"""initial schema"""
from alembic import op
import sqlalchemy as sa
revision="0001";down_revision=None;branch_labels=None;depends_on=None
def upgrade():
    from wecom_ai_gateway.db import Base
    from wecom_ai_gateway import models
    Base.metadata.create_all(bind=op.get_bind())
def downgrade():
    from wecom_ai_gateway.db import Base
    from wecom_ai_gateway import models
    Base.metadata.drop_all(bind=op.get_bind())
