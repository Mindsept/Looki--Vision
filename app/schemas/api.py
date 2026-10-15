from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    center_id: str = Field(
        default="demo-centre", min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$"
    )
    user_id: str | None = Field(default=None, max_length=100)


class SessionRead(BaseModel):
    id: UUID
    center_id: str
    user_id: str | None
    status: Literal["active", "closed"]
    created_at: datetime
    closed_at: datetime | None
    context: dict


class DetectionRead(BaseModel):
    label: str
    confidence: float
    bbox: list[float]
    distance_m: float | None
    distance_source: str
    proximity: Literal["near", "medium", "far", "unknown"]
    observation_type: Literal["region", "object"] = "region"
    depth_valid_fraction: float | None = None


class FrameRead(BaseModel):
    frame_id: UUID
    session_id: UUID
    status: Literal["pending", "processing", "completed", "failed"]
    captured_at: datetime
    received_at: datetime
    processed_at: datetime | None
    width: int
    height: int
    provider: str | None
    simulated: bool | None
    description: str | None
    error_code: str | None
    latency_ms: float | None
    timing_ms: dict[str, float] = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
    detections: list[DetectionRead] = Field(default_factory=list)


class ServerEvent(BaseModel):
    id: int | None
    type: str
    session_id: UUID
    timestamp: datetime
    payload: dict


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["question"]
    request_id: UUID
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def strip_text(cls, value):
        if not value.strip():
            raise ValueError("Question cannot be blank")
        return value.strip()


class Ping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["ping"]
