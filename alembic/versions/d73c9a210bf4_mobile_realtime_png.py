"""PNG ingestion and durable HTTP mobile audio lifecycle."""

import sqlalchemy as sa

from alembic import op

revision = "d73c9a210bf4"
down_revision = "c581a9367e02"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "frames",
        sa.Column("content_type", sa.String(32), nullable=False, server_default="image/jpeg"),
    )
    op.create_table(
        "realtime_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("offer_sha256", sa.String(64), nullable=False),
        sa.Column("media_epoch", sa.Integer(), nullable=False),
        sa.Column("control_status", sa.String(16), nullable=False),
        sa.Column("provider_call_id", sa.String(200)),
        sa.Column("sdp_answer", sa.Text()),
        sa.Column("error", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("session_id", "request_id", name="uq_call_request"),
        sa.UniqueConstraint("session_id", "media_epoch", name="uq_call_epoch"),
    )
    op.create_index("ix_realtime_calls_session_id", "realtime_calls", ["session_id"])
    op.create_index(
        "uq_call_active_session",
        "realtime_calls",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
        sqlite_where=sa.text("closed_at IS NULL"),
    )
    op.create_table(
        "audio_questions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("call_id", sa.Uuid(), sa.ForeignKey("realtime_calls.id"), nullable=False),
        sa.Column("media_epoch", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("media_status", sa.String(16), nullable=False),
        sa.Column("context_id", sa.Uuid()),
        sa.Column("context", sa.JSON()),
        sa.Column("provider_response_id", sa.String(200)),
        sa.Column("transcript", sa.Text()),
        sa.Column("error", sa.JSON()),
        sa.Column("playback", sa.JSON(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "request_id", name="uq_audio_question_request"),
    )
    op.create_index("ix_audio_questions_session_id", "audio_questions", ["session_id"])
    op.create_index(
        "uq_question_active_media",
        "audio_questions",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("media_status != 'closed'"),
        sqlite_where=sa.text("media_status != 'closed'"),
    )


def downgrade():
    op.drop_table("audio_questions")
    op.drop_table("realtime_calls")
    op.drop_column("frames", "content_type")
