from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.vision.depth_anything import DepthPrediction
from app.vision.yolo import YOLO_REVISION, YoloObjectsProvider


class Tensor:
    def __init__(self, value):
        self.value = np.asarray(value)

    def cpu(self):
        return self.value


class Detector:
    names = {0: "person", 1: "bicycle"}

    def predict(self, **kwargs):
        self.input = kwargs["source"]
        self.options = kwargs
        return [
            SimpleNamespace(
                names=self.names,
                boxes=SimpleNamespace(
                    xyxy=Tensor([[20, 10, 80, 90], [5, 5, 15, 20]]),
                    conf=Tensor([0.9, 0.2]),
                    cls=Tensor([0, 1]),
                ),
                speed={"preprocess": 1, "inference": 2, "postprocess": 3},
            )
        ]


class Depth:
    max_depth_m = 100

    def predict_depth(self, image):
        self.input = image
        return DepthPrediction(
            np.full((100, 100), 0.8, np.float32),
            {"depth_total_ms": 10},
            {"depth_convention": "z_depth"},
        )


def test_yolo_and_metric_depth_share_the_exact_image_and_preserve_raw_metres():
    detector, depth = Detector(), Depth()
    provider = YoloObjectsProvider("test-depth", depth_provider=depth, detector=detector)
    result = provider.analyze(Image.new("RGB", (100, 100)))
    assert detector.input is depth.input
    assert not result.simulated and result.provider == "yolo_objects"
    assert len(result.detections) == 1
    item = result.detections[0]
    assert item.label == "person" and item.bbox == (0.2, 0.1, 0.8, 0.9)
    assert item.distance_m == pytest.approx(0.8)
    assert item.observation_type == "object" and item.distance_source == "metric_model"
    assert result.metadata["object_model_revision"] == YOLO_REVISION
    assert result.timing_ms["detection_inference_ms"] == 2
    assert detector.options["save"] is False


def test_unusable_depth_remains_unknown_even_when_yolo_detects_an_object():
    class InvalidDepth(Depth):
        def predict_depth(self, image):
            return DepthPrediction(np.full((100, 100), np.nan), {}, {})

    result = YoloObjectsProvider(
        "test", depth_provider=InvalidDepth(), detector=Detector()
    ).analyze(Image.new("RGB", (100, 100)))
    assert result.detections[0].distance_m is None
    assert result.detections[0].depth_valid_fraction == 0


def test_live_resolution_setting_preserves_zero_and_accepts_empty_default():
    from app.core.config import Settings

    assert Settings(_env_file=None, unidepth_resolution_level=0).unidepth_resolution_level == 0
    assert Settings(_env_file=None, unidepth_resolution_level="").unidepth_resolution_level is None
    with pytest.raises(ValueError):
        Settings(_env_file=None, unidepth_resolution_level=10)
