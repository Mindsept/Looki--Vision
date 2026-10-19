"""Durable independent depth samples, tied to their original frame and masks."""

import sqlalchemy as sa

from alembic import op

revision = "c581a9367e02"
down_revision = "b84e901df8ac"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "depth_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "frame_id",
            sa.Uuid(),
            sa.ForeignKey("frames.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("masks", sa.LargeBinary(), nullable=False),
        sa.Column("observations", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')", name="ck_depth_job_status"
        ),
    )


def downgrade():
    op.drop_table("depth_jobs")
