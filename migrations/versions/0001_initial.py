"""initial schema"""
from alembic import op

revision="0001";down_revision=None;branch_labels=None;depends_on=None
def upgrade():
    from wecom_ai_gateway.db import Base
    Base.metadata.create_all(bind=op.get_bind())
def downgrade():
    from wecom_ai_gateway.db import Base
    Base.metadata.drop_all(bind=op.get_bind())
