from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.time import utcnow


class RealtimeCall(Base):
    __tablename__ = "realtime_calls"
    __table_args__ = (
        UniqueConstraint("session_id", "request_id", name="uq_call_request"),
        UniqueConstraint("session_id", "media_epoch", name="uq_call_epoch"),
        Index(
            "uq_call_active_session",
            "session_id",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
            sqlite_where=text("closed_at IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"), index=True)
    request_id: Mapped[UUID] = mapped_column(Uuid)
    offer_sha256: Mapped[str] = mapped_column(String(64))
    media_epoch: Mapped[int] = mapped_column(Integer)
    control_status: Mapped[str] = mapped_column(String(16), default="connecting")
    provider_call_id: Mapped[str | None] = mapped_column(String(200))
    sdp_answer: Mapped[str | None] = mapped_column(Text)
    error: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AudioQuestion(Base):
    __tablename__ = "audio_questions"
    __table_args__ = (
        UniqueConstraint("session_id", "request_id", name="uq_audio_question_request"),
        Index(
            "uq_question_active_media",
            "session_id",
            unique=True,
            postgresql_where=text("media_status != 'closed'"),
            sqlite_where=text("media_status != 'closed'"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"), index=True)
    request_id: Mapped[UUID] = mapped_column(Uuid)
    call_id: Mapped[UUID] = mapped_column(ForeignKey("realtime_calls.id"))
    media_epoch: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="accepted")
    media_status: Mapped[str] = mapped_column(String(16), default="reserved")
    context_id: Mapped[UUID | None] = mapped_column(Uuid)
    context: Mapped[dict | None] = mapped_column(JSON)
    provider_response_id: Mapped[str | None] = mapped_column(String(200))
    transcript: Mapped[str | None] = mapped_column(Text)
    generation: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[dict | None] = mapped_column(JSON)
    playback: Mapped[dict] = mapped_column(JSON, default=lambda: {"sequence": 0, "state": "ready"})
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
