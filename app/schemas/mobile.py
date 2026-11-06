from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool = False
    request_id: UUID | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


class CallCreate(StrictModel):
    request_id: UUID = Field(description="Idempotency key for this SDP offer; reuse on retry.")
    sdp_offer: str = Field(min_length=1, max_length=65536)

    @field_validator("sdp_offer")
    @classmethod
    def receive_only_audio(cls, value):
        lines = value.replace("\r\n", "\n").splitlines()
        if not lines or lines[0] != "v=0" or not any(s.startswith("o=") for s in lines):
            raise ValueError("Invalid SDP")
        sections = []
        for line in lines:
            if line.startswith("m="):
                if len(line.split()) < 4:
                    raise ValueError("Incomplete SDP media description")
                sections.append([line])
            elif sections:
                sections[-1].append(line)
        audio = [s for s in sections if s[0].startswith("m=audio ") and s[0].split()[1] != "0"]
        if not audio or any(
            "a=recvonly" not in s or any(v in s for v in ("a=sendrecv", "a=sendonly"))
            for s in audio
        ):
            raise ValueError("SDP must offer receive-only audio, without microphone track")
        if any(s[0].startswith("m=video ") and s[0].split()[1] != "0" for s in sections):
            raise ValueError("No video media track; images are supplied by the backend")
        return value


class CallRead(BaseModel):
    session_id: UUID
    call_id: UUID = Field(description="Opaque LOOKi call identifier, bound to one provider call.")
    request_id: UUID
    media_epoch: int
    control_status: Literal["connecting", "ready", "closing", "failed", "closed"]
    sdp_answer: str | None = None
    error: ErrorDetail | None = None
    created_at: datetime


class MediaIdentity(StrictModel):
    call_id: UUID
    media_epoch: int = Field(ge=1, strict=True)


class MobileQuestion(MediaIdentity):
    request_id: UUID
    text: str = Field(min_length=1, max_length=2000)
    playback_ready: Literal[True]

    @field_validator("text")
    @classmethod
    def normalize(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Question cannot be blank")
        return value


class PlaybackUpdate(MediaIdentity):
    sequence: int = Field(ge=1, strict=True)
    state: Literal["playing", "finished", "interrupted"]
    position_ms: int | None = Field(default=None, ge=0, strict=True)
    measurement: Literal["measured", "estimated", "unavailable"]

    @model_validator(mode="after")
    def valid_measurement(self):
        if (self.position_ms is None) != (self.measurement == "unavailable"):
            raise ValueError("position_ms and measurement must agree")
        return self


class PlaybackRead(BaseModel):
    sequence: int
    state: Literal["ready", "playing", "finished", "interrupted"]
    position_ms: int | None = None
    measurement: Literal["measured", "estimated", "unavailable"] = "unavailable"


class CancelRequest(MediaIdentity):
    reason: Literal[
        "user_stop", "new_question", "alert", "audio_interruption", "background", "connection_lost"
    ]


class AssistantRead(BaseModel):
    configuration_status: Literal["disabled", "missing_key", "configured"] = Field(
        description="Local configuration only; configured does not prove provider access or audio readiness."
    )
    input_mode: Literal["local_stt_text"] = "local_stt_text"
    model: str
    voice: str
    prompt_version: str
    reasoning_effort: str
    max_output_tokens: int
    scene_max_age_seconds: float
    max_scene_images: Literal[3] = 3
    question_interval_seconds: float


class TokenUsage(BaseModel):
    total_tokens: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    input_text_tokens: int | None = Field(default=None, ge=0)
    input_image_tokens: int | None = Field(default=None, ge=0)
    input_audio_tokens: int | None = Field(default=None, ge=0)
    input_cached_tokens: int | None = Field(default=None, ge=0)
    cached_text_tokens: int | None = Field(default=None, ge=0)
    cached_image_tokens: int | None = Field(default=None, ge=0)
    cached_audio_tokens: int | None = Field(default=None, ge=0)
    output_text_tokens: int | None = Field(default=None, ge=0)
    output_audio_tokens: int | None = Field(default=None, ge=0)


class GenerationRead(BaseModel):
    model: str
    voice: str
    prompt_version: str
    reasoning_effort: str
    max_output_tokens: int
    requested_at: datetime
    submitted_at: datetime | None = None
    provider_created_at: datetime | None = None
    provider_audio_started_at: datetime | None = Field(
        default=None,
        description="Server receipt of provider buffer-start event; not sound heard on iPhone.",
    )
    finished_at: datetime | None = None
    provider_status: Literal["completed", "cancelled", "failed", "incomplete"] | None = None
    usage: TokenUsage | None = None


class SceneSummary(BaseModel):
    frame_ids: list[UUID]
    frame_count: int = Field(ge=1, le=3)
    partial: bool
    simulated: bool
    oldest_capture_at: datetime
    newest_capture_at: datetime


class QuestionRead(BaseModel):
    session_id: UUID
    request_id: UUID
    call_id: UUID
    media_epoch: int
    status: Literal["accepted", "generating", "completed", "cancelled", "failed", "unavailable"]
    context_id: UUID | None
    transcript: str | None
    error: ErrorDetail | None
    playback: PlaybackRead
    media_status: Literal["reserved", "playing", "stopping", "closed"]
    updated_at: datetime
    sound_id: str | None = None
    scene: SceneSummary | None = None
    generation: GenerationRead | None = Field(
        default=None,
        description="Configuration and server observations. Null for pre-v1.3 history.",
    )


class AccessTokens(BaseModel):
    session_id: UUID
    mobile_token: str
    pi_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in_seconds: int
