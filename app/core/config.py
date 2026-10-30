from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOOKI_", env_file=".env", extra="ignore")

    api_key: str = Field(default="looki-local-dev-key", min_length=8)
    database_url: str | None = None
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "looki"
    db_user: str = "looki"
    db_password: str = "looki-local-password"
    create_schema: bool = False  # Test-only convenience. Production uses Alembic.
    worker_enabled: bool = True
    worker_poll_seconds: float = Field(default=0.2, ge=0.01)
    websocket_poll_seconds: float = Field(default=0.2, ge=0.01)
    vision_provider: Literal[
        "mock", "depth_anything_metric", "urban_objects", "yolo_objects", "yolo_seg_track"
    ] = "mock"
    depth_model_id: str | None = None
    depth_backend: Literal["depth_anything", "unidepth_v2"] = "depth_anything"
    camera_calibration_path: Path | None = None
    unidepth_resolution_level: int | None = Field(default=None, ge=0, le=9)
    yolo_image_size: int = Field(default=640, ge=320, le=1280, multiple_of=32)
    cpu_threads: int = Field(default=4, ge=1, le=64)
    decoupled_depth: bool = True
    depth_interval_seconds: float = Field(default=2.0, ge=0.5, le=60)
    depth_max_age_seconds: float = Field(default=3.0, ge=0.5, le=30)
    demo_video_path: Path | None = None
    object_model_id: str = "PekingU/rtdetr_r18vd"
    object_confidence_threshold: float = Field(default=0.5, ge=0, le=1)
    vision_device: str = "cpu"
    near_threshold_m: float = Field(default=1.5, gt=0)
    far_threshold_m: float = Field(default=3.0, gt=0)
    min_confidence: float = Field(default=0.5, ge=0, le=1)
    max_pending_frames: int = Field(default=32, ge=1)
    max_image_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    max_request_bytes: int = Field(default=6 * 1024 * 1024, ge=1024)
    max_image_pixels: int = Field(default=12_000_000, ge=1)
    max_frame_age_seconds: int = Field(default=30, ge=1)
    max_future_seconds: int = Field(default=5, ge=0)
    centres_provider: Literal["mock", "rest"] = "mock"
    centres_base_url: str = ""
    centres_api_key: str = ""
    centres_timeout_seconds: float = Field(default=3.0, gt=0)
    fixtures_dir: Path = Path(__file__).resolve().parents[2] / "fixtures"
    session_token_ttl_seconds: int = Field(default=28800, ge=60, le=86400)
    alert_ttl_seconds: float = Field(default=3.0, ge=0.5, le=10)
    realtime_enabled: bool = False
    openai_api_key: SecretStr = SecretStr("")
    realtime_model: str = "gpt-realtime"
    realtime_voice: str = "marin"
    realtime_connect_timeout_seconds: float = Field(default=20, ge=1, le=120)
    realtime_generation_timeout_seconds: float = Field(default=30, ge=1, le=120)
    realtime_playback_timeout_seconds: float = Field(default=60, ge=1, le=300)
    realtime_call_max_seconds: float = Field(default=900, ge=10, le=3600)
    scene_max_age_seconds: float = Field(default=3.0, ge=0.5, le=30)

    @field_validator(
        "depth_model_id",
        "camera_calibration_path",
        "demo_video_path",
        "unidepth_resolution_level",
        mode="before",
    )
    @classmethod
    def empty_depth_model_uses_provider_default(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_limits(self):
        if self.camera_calibration_path is not None and (
            self.vision_provider not in {"urban_objects", "yolo_objects", "yolo_seg_track"}
            or self.depth_backend != "unidepth_v2"
        ):
            raise ValueError(
                "Camera calibration requires urban_objects or yolo_objects with unidepth_v2"
            )
        if self.far_threshold_m <= self.near_threshold_m:
            raise ValueError("far_threshold_m must exceed near_threshold_m")
        if self.max_request_bytes <= self.max_image_bytes:
            raise ValueError("max_request_bytes must exceed max_image_bytes")
        if self.centres_provider == "rest" and not self.centres_base_url.startswith(
            ("http://", "https://")
        ):
            raise ValueError("centres_base_url must be an HTTP(S) URL")
        return self

    @property
    def sqlalchemy_url(self) -> str | URL:
        return self.database_url or URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )
