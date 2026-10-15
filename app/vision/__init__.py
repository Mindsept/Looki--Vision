"""Providers interchangeables pour le POC LOOKi."""

from pathlib import Path

from app.vision.base import DetectionResult, VisionProvider, VisionResult
from app.vision.depth_anything import (
    DEFAULT_MODEL_ID,
    DEFAULT_OUTDOOR_MODEL_ID,
    DepthAnythingMetricProvider,
    DepthPrediction,
)
from app.vision.mock import MockVisionProvider
from app.vision.urban import DEFAULT_OBJECT_MODEL_ID, UrbanObjectsProvider


def create_provider(
    name: str,
    model_id: str | None = None,
    device: str = "cpu",
    *,
    object_model_id: str = DEFAULT_OBJECT_MODEL_ID,
    object_confidence_threshold: float = 0.5,
    depth_backend: str = "depth_anything",
    camera_calibration_path: Path | None = None,
) -> VisionProvider:
    """Construire un provider explicite; aucune stratégie de fallback implicite."""
    if camera_calibration_path is not None and (
        name != "urban_objects" or depth_backend != "unidepth_v2"
    ):
        raise ValueError("Camera calibration requires urban_objects with unidepth_v2")
    if name == "mock":
        return MockVisionProvider()
    if name == "depth_anything_metric":
        return DepthAnythingMetricProvider(model_id=model_id or DEFAULT_MODEL_ID, device=device)
    if name == "urban_objects":
        if depth_backend == "unidepth_v2":
            from app.vision.calibration import CameraCalibration
            from app.vision.unidepth import DEFAULT_UNIDEPTH_MODEL_ID, UniDepthMetricProvider

            selected_model = model_id or DEFAULT_UNIDEPTH_MODEL_ID
            calibration = (
                CameraCalibration.from_json(camera_calibration_path)
                if camera_calibration_path is not None
                else None
            )
            depth_provider = UniDepthMetricProvider(
                model_id=selected_model, device=device, camera_calibration=calibration
            )
        elif depth_backend == "depth_anything":
            selected_model = model_id or DEFAULT_OUTDOOR_MODEL_ID
            depth_provider = None
        else:
            raise ValueError(f"Unknown depth backend: {depth_backend}")
        return UrbanObjectsProvider(
            depth_model_id=selected_model,
            object_model_id=object_model_id,
            device=device,
            object_confidence_threshold=object_confidence_threshold,
            depth_provider=depth_provider,
        )
    raise ValueError(f"Unknown vision provider: {name}")


__all__ = [
    "DEFAULT_MODEL_ID",
    "DEFAULT_OBJECT_MODEL_ID",
    "DEFAULT_OUTDOOR_MODEL_ID",
    "DepthAnythingMetricProvider",
    "DepthPrediction",
    "DetectionResult",
    "MockVisionProvider",
    "VisionProvider",
    "VisionResult",
    "UrbanObjectsProvider",
    "create_provider",
]
