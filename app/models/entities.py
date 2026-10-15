from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.time import utcnow


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (CheckConstraint("status IN ('active', 'closed')", name="ck_session_status"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    center_id: Mapped[str] = mapped_column(String(100))
    user_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default="active")
    context: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Frame(Base):
    __tablename__ = "frames"
    __table_args__ = (
        UniqueConstraint("session_id", "frame_id", name="uq_frame_session_client"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')", name="ck_frame_status"
        ),
        Index("ix_frames_queue", "status", "received_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    frame_id: Mapped[UUID] = mapped_column(Uuid)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    sha256: Mapped[str] = mapped_column(String(64))
    image: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str | None] = mapped_column(String(100))
    simulated: Mapped[bool | None]
    description: Mapped[str | None] = mapped_column(String(2000))
    error_code: Mapped[str | None] = mapped_column(String(100))
    latency_ms: Mapped[float | None] = mapped_column(Float)
    timing_ms: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    vision_metadata: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class Detection(Base):
    __tablename__ = "detections"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_detection_confidence"),
        CheckConstraint("distance_m IS NULL OR distance_m >= 0", name="ck_detection_distance"),
        CheckConstraint(
            "proximity IN ('near', 'medium', 'far', 'unknown')", name="ck_detection_proximity"
        ),
        CheckConstraint("observation_type IN ('region', 'object')", name="ck_observation_type"),
        CheckConstraint(
            "depth_valid_fraction IS NULL OR (depth_valid_fraction >= 0 AND depth_valid_fraction <= 1)",
            name="ck_depth_valid_fraction",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    frame_id: Mapped[UUID] = mapped_column(ForeignKey("frames.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(100))
    confidence: Mapped[float] = mapped_column(Float)
    bbox: Mapped[list] = mapped_column(JSON)
    distance_m: Mapped[float | None] = mapped_column(Float)
    distance_source: Mapped[str] = mapped_column(String(32))
    proximity: Mapped[str] = mapped_column(String(16))
    observation_type: Mapped[str] = mapped_column(
        String(16), default="region", server_default="region"
    )
    depth_valid_fraction: Mapped[float | None] = mapped_column(Float)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_session_cursor", "session_id", "id"),
        UniqueConstraint("session_id", "request_id", name="uq_event_question_request"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    frame_id: Mapped[UUID | None] = mapped_column(ForeignKey("frames.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    payload: Mapped[dict] = mapped_column(JSON)
    request_id: Mapped[UUID | None] = mapped_column(Uuid)
