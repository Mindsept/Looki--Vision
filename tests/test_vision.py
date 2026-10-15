"""Tests de vision sans réseau, sans téléchargement de poids et sans torch."""

from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.vision import DetectionResult, MockVisionProvider, create_provider
from app.vision.depth_anything import (
    DepthAnythingMetricProvider,
    summarize_metric_depth,
    validate_metric_config,
)
from app.vision.mock import create_mock_image


@pytest.mark.parametrize("scene,distance", [("near", 0.45), ("far", 3.5), ("clear", None)])
def test_committed_jpeg_scenarios(scene, distance):
    path = Path(__file__).resolve().parents[1] / "fixtures" / "images" / f"{scene}.jpg"
    with Image.open(path) as image:
        assert image.format == "JPEG"
        result = MockVisionProvider().analyze(image)
    assert result.simulated is True
    assert result.provider == "mock"
    if distance is None:
        assert result.detections == []
    else:
        assert result.detections[0].distance_m == distance
        assert result.detections[0].distance_source == "simulated"
        assert result.detections[0].label == "obstacle_mock"


@pytest.mark.parametrize("scene", ["near", "far", "clear"])
def test_mock_tolerates_jpeg_reencoding_and_resize(scene):
    image = create_mock_image(scene, size=(320, 240))
    stream = BytesIO()
    image.save(stream, format="JPEG", quality=75)
    stream.seek(0)
    with Image.open(stream) as decoded:
        result = MockVisionProvider().analyze(decoded)
    assert "hors fixtures" not in result.description


@pytest.mark.parametrize("color", ["red", "blue", "black", "white"])
def test_mock_never_invents_distance_for_arbitrary_images(color):
    image = Image.new("RGB", (640, 480), color)
    result = MockVisionProvider().analyze(image)
    assert result.detections == []
    assert "ne permet pas d'évaluer" in result.description
    assert result.simulated is True


def test_mock_rejects_copied_signature_with_unrecognized_scene():
    image = create_mock_image("near")
    ImageDraw.Draw(image).rectangle((0, 100, 639, 479), fill="red")
    assert MockVisionProvider().analyze(image).detections == []


def test_mock_is_deterministic_and_results_are_immutable():
    provider = MockVisionProvider()
    first = provider.analyze(create_mock_image("near"))
    assert first == provider.analyze(create_mock_image("near"))
    with pytest.raises(FrozenInstanceError):
        first.simulated = False


@pytest.mark.parametrize("distance", [float("nan"), float("inf"), -0.1, 0])
def test_detection_rejects_non_physical_distances(distance):
    with pytest.raises(ValueError, match="distance"):
        DetectionResult("obstacle", 1.0, (0.0, 0.0, 1.0, 1.0), distance)


def test_unknown_distance_is_allowed_and_source_is_explicit():
    result = DetectionResult("obstacle", 0.7, (0.0, 0.0, 1.0, 1.0), None, "metric_model")
    assert result.distance_m is None


def test_metric_reduction_keeps_raw_meters_without_visualization_scaling():
    depth = np.full((100, 100), 10.0, dtype=np.float32)
    depth[:, 5:35] = 0.45
    depth[:, 35:65] = 3.5
    results = summarize_metric_depth(depth, max_depth_m=20)
    assert [item.distance_m for item in results] == pytest.approx([0.45, 3.5, 10.0])
    assert [item.label for item in results] == [
        "obstacle_left",
        "obstacle_center",
        "obstacle_right",
    ]
    assert all(item.distance_source == "metric_model" for item in results)
    assert all(item.confidence == 1.0 for item in results)


def test_percentile_ignores_one_spurious_close_pixel():
    depth = np.full((100, 100), 3.5, dtype=np.float32)
    depth[50, 50] = 0.01
    assert all(item.distance_m == 3.5 for item in summarize_metric_depth(depth, 20))


def test_invalid_values_do_not_escape_as_nan_or_infinity():
    depth = np.full((100, 100), 4.0, dtype=np.float32)
    depth[25, 20] = np.nan
    depth[26, 20] = np.inf
    depth[27, 20] = -np.inf
    depth[28, 20] = 0
    depth[29, 20] = -1
    depth[30, 20] = 21  # Outside the model's metric range.
    results = summarize_metric_depth(depth, 20)
    assert len(results) == 3
    assert results[0].distance_m == 4
    assert 0.5 < results[0].confidence < 1


@pytest.mark.parametrize("invalid", [np.nan, np.inf, 0.0, -1.0, 21.0])
def test_inadequate_depth_reports_no_observation(invalid):
    assert summarize_metric_depth(np.full((40, 40), invalid), 20) == []


@pytest.mark.parametrize("shape", [(3, 3), (1, 8, 8), (8,)])
def test_depth_map_shape_is_checked(shape):
    with pytest.raises(ValueError, match="two-dimensional"):
        summarize_metric_depth(np.ones(shape), 20)


@pytest.mark.parametrize("depth_type", ["relative", None, "unknown"])
def test_rejects_relative_checkpoint(depth_type):
    config = SimpleNamespace(
        model_type="depth_anything", depth_estimation_type=depth_type, max_depth=20
    )
    with pytest.raises(ValueError, match="metric"):
        validate_metric_config(config)


@pytest.mark.parametrize("maximum", [None, 0, -1, float("inf"), float("nan")])
def test_metric_checkpoint_requires_valid_maximum(maximum):
    config = SimpleNamespace(
        model_type="depth_anything", depth_estimation_type="metric", max_depth=maximum
    )
    with pytest.raises(ValueError, match="max_depth"):
        validate_metric_config(config)


class FakeTensor:
    def __init__(self, value):
        self.value = np.asarray(value)

    def to(self, device):
        assert device == "cpu"
        return self

    def detach(self):
        return self

    def float(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.value


class FakeModel:
    def __init__(self, output, depth_type="metric"):
        self.config = SimpleNamespace(
            model_type="depth_anything",
            depth_estimation_type=depth_type,
            max_depth=20,
        )
        self.output = output
        self.evaluating = False
        self.calls = 0

    def to(self, device):
        assert device == "cpu"
        return self

    def eval(self):
        self.evaluating = True
        return self

    def __call__(self, **inputs):
        assert self.evaluating
        assert "pixel_values" in inputs
        self.calls += 1
        return SimpleNamespace(predicted_depth=FakeTensor(self.output))


def fake_processor(*, images, return_tensors):
    assert isinstance(images, Image.Image)
    assert images.mode == "RGB"
    assert return_tensors == "pt"
    return {"pixel_values": FakeTensor(np.zeros((1, 3, 40, 40)))}


def test_local_adapter_reads_raw_model_output_without_loading_weights():
    model = FakeModel(np.full((1, 40, 40), 6.25))
    provider = DepthAnythingMetricProvider(
        processor=fake_processor,
        model=model,
        torch_module=SimpleNamespace(inference_mode=nullcontext),
    )
    result = provider.analyze(Image.new("RGBA", (80, 80)))
    assert model.calls == 1
    assert result.simulated is False
    assert result.provider == "depth_anything_metric"
    assert [item.distance_m for item in result.detections] == [6.25, 6.25, 6.25]


def test_adapter_refuses_relative_model_even_when_injected():
    with pytest.raises(ValueError, match="metric"):
        DepthAnythingMetricProvider(
            processor=fake_processor,
            model=FakeModel(np.ones((1, 40, 40)), depth_type="relative"),
            torch_module=SimpleNamespace(inference_mode=nullcontext),
        )


def test_adapter_does_not_hide_invalid_model_output():
    provider = DepthAnythingMetricProvider(
        processor=fake_processor,
        model=FakeModel(np.ones((2, 40, 40))),
        torch_module=SimpleNamespace(inference_mode=nullcontext),
    )
    with pytest.raises(ValueError, match="batch/shape"):
        provider.analyze(Image.new("RGB", (80, 80)))


def test_factory_rejects_unknown_provider_instead_of_silent_fallback():
    assert isinstance(create_provider("mock"), MockVisionProvider)
    with pytest.raises(ValueError, match="Unknown vision provider"):
        create_provider("depth_typo")
