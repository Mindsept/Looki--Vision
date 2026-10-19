"""Persist instance masks, tracking identifiers and possible proximity evidence."""

import sqlalchemy as sa

from alembic import op

revision = "b84e901df8ac"
down_revision = "9d21c735a842"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "detections", sa.Column("perception", sa.JSON(), server_default="{}", nullable=False)
    )


def downgrade():
    op.drop_column("detections", "perception")
