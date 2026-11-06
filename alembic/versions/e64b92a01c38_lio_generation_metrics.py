"""Persist Lio configuration and bounded generation measurements per question."""

import sqlalchemy as sa

from alembic import op

revision = "e64b92a01c38"
down_revision = "d73c9a210bf4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("audio_questions", sa.Column("generation", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("audio_questions", "generation")
