"""Persist object observations and measured vision stage timings.

Revision ID: 9d21c735a842
Revises: 6b8319947aa6
"""

import sqlalchemy as sa

from alembic import op

revision = "9d21c735a842"
down_revision = "6b8319947aa6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("frames", sa.Column("timing_ms", sa.JSON(), server_default="{}", nullable=False))
    op.add_column(
        "frames", sa.Column("vision_metadata", sa.JSON(), server_default="{}", nullable=False)
    )
    op.add_column(
        "detections",
        sa.Column("observation_type", sa.String(16), server_default="region", nullable=False),
    )
    op.add_column("detections", sa.Column("depth_valid_fraction", sa.Float(), nullable=True))
    op.create_check_constraint(
        "ck_observation_type", "detections", "observation_type IN ('region', 'object')"
    )
    op.create_check_constraint(
        "ck_depth_valid_fraction",
        "detections",
        "depth_valid_fraction IS NULL OR (depth_valid_fraction >= 0 AND depth_valid_fraction <= 1)",
    )


def downgrade():
    op.drop_constraint("ck_depth_valid_fraction", "detections", type_="check")
    op.drop_constraint("ck_observation_type", "detections", type_="check")
    op.drop_column("detections", "depth_valid_fraction")
    op.drop_column("detections", "observation_type")
    op.drop_column("frames", "vision_metadata")
    op.drop_column("frames", "timing_ms")
