"""Fusion objets/profondeur et instrumentation, sans poids ni réseau."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app import vision
from app.vision import DetectionResult, VisionResult, depth_anything, timing
from app.vision.depth_anything import DepthAnythingMetricProvider, DepthPrediction
from app.vision.urban import (
    UrbanObjectsProvider,
    fuse_object_depth,
    normalize_object_bbox,
    object_depth,
)


def fuse(boxes, scores=None, labels=None, depth=None, id2label=None):
    count = len(boxes)
    return fuse_object_depth(
        boxes=np.asarray(boxes).reshape((-1, 4)),
        scores=np.asarray(scores if scores is not None else [0.95] * count),
        labels=np.asarray(labels if labels is not None else [0] * count),
        image_size=(200, 100),
        depth_m=np.full((20, 40), 2.5) if depth is None else depth,
        max_depth_m=80,
        id2label=id2label or {0: "person"},
    )


def test_pixel_boxes_map_to_depth_resolution_without_swapping_axes():
    depth = np.full((20, 40), 12.0)
    # Box x=[.1,.5], y=[.1,.9], central crop x=[.14,.46], y=[.18,.82].
    depth[3:17, 5:19] = 0.8
    result = fuse([[20, 10, 100, 90]], scores=[0.87], depth=depth)[0]
    assert result.bbox == (0.1, 0.1, 0.5, 0.9)
    assert result.distance_m == pytest.approx(0.8)
    assert result.observation_type == "object"
    assert result.label == "person"
    assert result.confidence == 0.87
    assert result.depth_valid_fraction == 1.0


def test_central_box_excludes_close_background_borders_and_uses_robust_percentile():
    depth = np.full((100, 100), 0.1)
    depth[10:90, 10:90] = 4.0
    depth[50, 50] = 0.01  # One outlier must not replace the object estimate.
    distance, coverage = object_depth(depth, (0, 0, 1, 1), 80)
    assert distance == 4.0
    assert coverage == 1.0


def test_bbox_is_clipped_without_reporting_out_of_frame_objects():
    result = fuse([[-20, -10, 220, 110], [210, 0, 250, 100]])
    assert len(result) == 1
    assert result[0].bbox == (0, 0, 1, 1)


@pytest.mark.parametrize(
    "box",
    [
        [10, 10, 10, 30],
        [10, 20, 40, 20],
        [30, 10, 20, 90],
        [-100, 0, -10, 50],
        [0, 110, 30, 120],
        [np.nan, 10, 30, 90],
        [0, 0, np.inf, 100],
    ],
)
def test_invalid_or_zero_area_boxes_are_rejected(box):
    assert normalize_object_bbox(box, (200, 100)) is None
    assert fuse([box]) == []


@pytest.mark.parametrize("score", [0.49, -0.1, 1.01, np.nan, np.inf])
def test_invalid_and_low_detection_scores_are_not_depth_observations(score):
    assert fuse([[0, 0, 200, 100]], scores=[score]) == []


def test_unknown_depth_keeps_object_identity_and_detection_score():
    depth = np.full((20, 40), np.nan)
    depth[2:10, 4:20] = 1.0  # Only one quarter of the central crop is usable.
    result = fuse([[0, 0, 200, 100]], scores=[0.93], depth=depth)[0]
    assert result.label == "person"
    assert result.distance_m is None
    assert result.confidence == 0.93
    assert result.depth_valid_fraction == 0.25


def test_exactly_half_valid_depth_is_accepted():
    depth = np.full((20, 40), np.nan)
    depth[:, :20] = 1.25
    result = fuse([[0, 0, 200, 100]], depth=depth)[0]
    assert result.distance_m == 1.25
    assert result.depth_valid_fraction == 0.5


@pytest.mark.parametrize("value", [np.nan, np.inf, -np.inf, -1, 0, 80.01])
def test_non_physical_depth_is_unknown_without_losing_detection(value):
    result = fuse([[0, 0, 200, 100]], depth=np.full((20, 40), value))[0]
    assert result.distance_m is None
    assert result.depth_valid_fraction == 0


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("motorbike", "motorcycle"),
        ("aeroplane", "airplane"),
        ("sofa", "couch"),
        ("pottedplant", "potted plant"),
        ("diningtable", "dining table"),
        ("tvmonitor", "tv"),
    ],
)
def test_checkpoint_aliases_are_canonical_coco_labels(alias, canonical):
    result = fuse([[0, 0, 200, 100]], id2label={"0": alias})
    assert result[0].label == canonical


def test_unknown_label_is_not_invented_and_empty_output_has_no_region_fallback():
    assert fuse([[0, 0, 200, 100]], labels=[99]) == []
    assert fuse([]) == []


def test_mismatched_model_outputs_fail_instead_of_silent_zip_truncation():
    with pytest.raises(ValueError, match="matching lengths"):
        fuse([[0, 0, 200, 100]], scores=[])


@pytest.mark.parametrize("value", [-0.01, 1.01, np.nan, np.inf])
def test_depth_coverage_contract_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="Depth valid fraction"):
        DetectionResult("car", 0.9, (0, 0, 1, 1), None, depth_valid_fraction=value)


def test_vision_result_dictionary_defaults_are_not_shared():
    first = VisionResult("mock", [], "first", True)
    second = VisionResult("mock", [], "second", True)
    first.timing_ms["test"] = 1
    first.metadata["test"] = 1
    assert second.timing_ms == {}
    assert second.metadata == {}


class Tensor:
    def __init__(self, values, clock=None):
        self.values = np.asarray(values)
        self.clock = clock

    def to(self, _):
        return self

    def detach(self):
        return self

    def float(self):
        return self

    def cpu(self):
        if self.clock:
            self.clock.now += 0.1
        return self

    def numpy(self):
        return self.values


class DetectorProcessor:
    def __init__(self, scores=(0.92, 0.4)):
        self.scores = scores

    def __call__(self, *, images, return_tensors):
        assert images.mode == "RGB"
        assert return_tensors == "pt"
        return {"pixel_values": Tensor([1])}

    def post_process_object_detection(self, outputs, *, target_sizes, threshold):
        assert outputs == "fake-output"
        assert target_sizes == [(100, 200)]  # HF target_sizes uses H,W, not W,H.
        assert threshold == 0.0
        return [
            {
                "boxes": Tensor([[20, 10, 100, 90], [40, 30, 70, 70]]),
                "scores": Tensor(self.scores),
                "labels": Tensor([0, 1]),
            }
        ]


class DetectorModel:
    config = SimpleNamespace(
        model_type="rt_detr",
        id2label={0: "person", 1: "motorbike"},
        _commit_hash="object-test",
    )

    def to(self, _):
        return self

    def eval(self):
        return self

    def __call__(self, **inputs):
        assert "pixel_values" in inputs
        return "fake-output"


class DepthProvider:
    max_depth_m = 80
    model_revision = "depth-test"

    def predict_depth(self, image):
        assert image.size == (200, 100)
        return DepthPrediction(
            np.full((20, 40), 0.8),
            {
                "depth_preprocess_ms": 1.0,
                "depth_inference_ms": 2.0,
                "depth_postprocess_ms": 3.0,
                "depth_total_ms": 6.0,
            },
        )


def test_urban_adapter_returns_real_objects_and_separate_depth_timings_without_download():
    provider = UrbanObjectsProvider(
        depth_provider=DepthProvider(),
        detector_processor=DetectorProcessor(),
        detector_model=DetectorModel(),
        torch_module=SimpleNamespace(inference_mode=nullcontext),
    )
    result = provider.analyze(Image.new("RGBA", (200, 100)))
    assert result.provider == "urban_objects"
    assert result.simulated is False
    assert len(result.detections) == 1
    assert result.detections[0].label == "person"
    assert result.detections[0].observation_type == "object"
    assert result.detections[0].distance_m == pytest.approx(0.8)
    assert result.metadata["depth_max_m"] == 80
    assert result.metadata["depth_percentile"] == 20
    assert result.metadata["object_model_revision"] == "object-test"
    assert result.metadata["confidence_semantics"] == "object_detection_score"
    assert result.timing_ms["depth_inference_ms"] == 2
    assert result.timing_ms["depth_total_ms"] == 6
    assert all(value >= 0 for value in result.timing_ms.values())
    assert result.timing_ms["detection_total_ms"] == pytest.approx(
        sum(
            result.timing_ms[key]
            for key in (
                "detection_preprocess_ms",
                "detection_inference_ms",
                "detection_postprocess_ms",
            )
        )
    )


def test_urban_adapter_never_falls_back_to_three_generic_regions():
    provider = UrbanObjectsProvider(
        depth_provider=DepthProvider(),
        detector_processor=DetectorProcessor(scores=(0.1, 0.2)),
        detector_model=DetectorModel(),
        torch_module=SimpleNamespace(inference_mode=nullcontext),
    )
    result = provider.analyze(Image.new("RGB", (200, 100)))
    assert result.detections == []
    assert "ne prouve pas" in result.description


def test_depth_inference_time_excludes_processor_and_tensor_transfer(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(depth_anything, "perf_counter", lambda: clock.now)
    monkeypatch.setattr(timing, "perf_counter", lambda: clock.now)

    def processor(**_):
        clock.now += 0.2
        return {"pixel_values": Tensor([1])}

    class Model(DetectorModel):
        config = SimpleNamespace(
            model_type="depth_anything", depth_estimation_type="metric", max_depth=80
        )

        def __call__(self, **_):
            clock.now += 0.4
            return SimpleNamespace(predicted_depth=Tensor(np.full((1, 10, 20), 2), clock=clock))

    provider = DepthAnythingMetricProvider(
        processor=processor,
        model=Model(),
        torch_module=SimpleNamespace(inference_mode=nullcontext),
    )
    prediction = provider.predict_depth(Image.new("RGB", (200, 100)))
    assert prediction.depth_m.shape == (10, 20)
    assert prediction.timing_ms == pytest.approx(
        {
            "depth_preprocess_ms": 200,
            "depth_inference_ms": 400,
            "depth_postprocess_ms": 100,
            "depth_total_ms": 700,
        }
    )


def test_gpu_timing_synchronizes_accelerator_and_cpu_needs_no_gpu_api():
    calls = []
    fake = SimpleNamespace(
        cuda=SimpleNamespace(synchronize=lambda device: calls.append(device)),
        mps=SimpleNamespace(synchronize=lambda: calls.append("mps")),
    )
    timing.synchronize_device(fake, "cuda:1")
    timing.synchronize_device(fake, "mps")
    timing.synchronize_device(SimpleNamespace(), "cpu")
    assert calls == ["cuda:1", "mps"]


def test_factory_uses_outdoor_model_for_urban_and_forwards_detector_configuration(monkeypatch):
    captured = {}

    def fake_factory(**kwargs):
        captured.update(kwargs)
        return "provider"

    monkeypatch.setattr(vision, "UrbanObjectsProvider", fake_factory)
    assert vision.create_provider("urban_objects", object_confidence_threshold=0.7) == "provider"
    assert captured["depth_model_id"] == vision.DEFAULT_OUTDOOR_MODEL_ID
    assert captured["object_model_id"] == "PekingU/rtdetr_r18vd"
    assert captured["object_confidence_threshold"] == 0.7


@pytest.mark.parametrize("custom_model", [None, "custom/unidepth-v2-model"])
def test_factory_selects_unidepth_explicitly_without_loading_any_weights(monkeypatch, custom_model):
    from app.vision import unidepth

    captured_depth, captured_urban = {}, {}
    sentinel = object()

    def fake_depth(**kwargs):
        captured_depth.update(kwargs)
        return sentinel

    def fake_urban(**kwargs):
        captured_urban.update(kwargs)
        return "urban-with-unidepth"

    monkeypatch.setattr(unidepth, "UniDepthMetricProvider", fake_depth)
    monkeypatch.setattr(vision, "UrbanObjectsProvider", fake_urban)
    result = vision.create_provider(
        "urban_objects",
        model_id=custom_model,
        depth_backend="unidepth_v2",
        device="cpu",
        object_model_id="custom/detector",
        object_confidence_threshold=0.65,
    )
    assert result == "urban-with-unidepth"
    assert captured_depth == {
        "model_id": custom_model or unidepth.DEFAULT_UNIDEPTH_MODEL_ID,
        "device": "cpu",
        "camera_calibration": None,
    }
    assert captured_urban["depth_provider"] is sentinel
    assert captured_urban["depth_model_id"] == captured_depth["model_id"]
    assert captured_urban["object_model_id"] == "custom/detector"
    assert captured_urban["object_confidence_threshold"] == 0.65


def test_invalid_depth_backend_fails_without_implicit_fallback(monkeypatch):
    monkeypatch.setattr(
        vision, "UrbanObjectsProvider", lambda **_: pytest.fail("Must not construct fallback")
    )
    with pytest.raises(ValueError, match="Unknown depth backend"):
        vision.create_provider("urban_objects", depth_backend="unknown")


def test_settings_accept_only_explicit_supported_depth_backend(monkeypatch):
    from pydantic import ValidationError

    from app.core.config import Settings

    monkeypatch.setenv("LOOKI_DEPTH_BACKEND", "unidepth_v2")
    assert Settings(_env_file=None).depth_backend == "unidepth_v2"
    with pytest.raises(ValidationError, match="depth_backend"):
        Settings(_env_file=None, depth_backend="unknown")


def test_urban_adapter_forwards_estimated_camera_metadata_and_raw_depth_timing():
    camera = [[500, 0, 100], [0, 500, 50], [0, 0, 1]]

    class UniDepthStub(DepthProvider):
        model_revision = "unidepth-fixed-revision"
        max_depth_m = 10_000

        def predict_depth(self, image):
            prediction = super().predict_depth(image)
            return DepthPrediction(
                prediction.depth_m,
                prediction.timing_ms,
                metadata={
                    "depth_backend": "unidepth_v2",
                    "depth_intrinsics": camera,
                    "depth_intrinsics_source": "estimated",
                    "depth_semantics": "camera z in meters; not Euclidean range",
                    "depth_resolution_level": 9,
                    "depth_timing_scope": "official infer including internal resize",
                },
            )

    provider = UrbanObjectsProvider(
        depth_model_id="lpiccinelli/unidepth-v2-vits14",
        depth_provider=UniDepthStub(),
        detector_processor=DetectorProcessor(),
        detector_model=DetectorModel(),
        torch_module=SimpleNamespace(inference_mode=nullcontext),
    )
    result = provider.analyze(Image.new("RGB", (200, 100)))
    assert result.metadata["depth_model_id"] == "lpiccinelli/unidepth-v2-vits14"
    assert result.metadata["depth_model_revision"] == "unidepth-fixed-revision"
    assert result.metadata["depth_backend"] == "unidepth_v2"
    assert result.metadata["depth_intrinsics"] == camera
    assert result.metadata["depth_intrinsics_source"] == "estimated"
    assert result.metadata["depth_resolution_level"] == 9
    assert result.metadata["depth_timing_scope"] == "official infer including internal resize"
    assert result.metadata["depth_max_m"] == 10_000
    assert result.timing_ms["depth_inference_ms"] == 2
    assert result.detections[0].distance_m == pytest.approx(0.8)


def test_factory_loads_provided_camera_file_and_passes_calibration_to_unidepth(
    tmp_path, monkeypatch
):
    from app.vision import unidepth
    from app.vision.calibration import CameraCalibration

    path = tmp_path / "camera.json"
    path.write_text(
        json.dumps({"width": 640, "height": 480, "fx": 510, "fy": 505, "cx": 320, "cy": 240})
    )
    captured = {}
    sentinel = object()

    def fake_depth(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(unidepth, "UniDepthMetricProvider", fake_depth)
    monkeypatch.setattr(vision, "UrbanObjectsProvider", lambda **kwargs: kwargs)
    result = vision.create_provider(
        "urban_objects", depth_backend="unidepth_v2", camera_calibration_path=path
    )
    calibration = captured["camera_calibration"]
    assert isinstance(calibration, CameraCalibration)
    assert (calibration.width, calibration.height) == (640, 480)
    assert calibration.fx == 510
    assert calibration.fy == 505
    assert calibration.cx == 320
    assert calibration.cy == 240
    assert result["depth_provider"] is sentinel


@pytest.mark.parametrize(
    "contents",
    ["not JSON", '{"width":640}', '{"width":640,"height":480,"fx":0,"fy":500,"cx":320,"cy":240}'],
)
def test_factory_rejects_bad_calibration_before_model_loading(tmp_path, monkeypatch, contents):
    from app.vision import unidepth

    path = tmp_path / "camera.json"
    path.write_text(contents)
    monkeypatch.setattr(
        unidepth,
        "UniDepthMetricProvider",
        lambda **_: pytest.fail("Invalid camera must fail before model construction"),
    )
    with pytest.raises(ValueError):
        vision.create_provider(
            "urban_objects", depth_backend="unidepth_v2", camera_calibration_path=path
        )


def test_factory_rejects_missing_calibration_file_before_model_loading(tmp_path, monkeypatch):
    from app.vision import unidepth

    monkeypatch.setattr(
        unidepth,
        "UniDepthMetricProvider",
        lambda **_: pytest.fail("Missing camera must not become estimated calibration"),
    )
    with pytest.raises(FileNotFoundError):
        vision.create_provider(
            "urban_objects",
            depth_backend="unidepth_v2",
            camera_calibration_path=tmp_path / "absent.json",
        )


@pytest.mark.parametrize(
    "provider,backend",
    [
        ("mock", "unidepth_v2"),
        ("depth_anything_metric", "unidepth_v2"),
        ("urban_objects", "depth_anything"),
    ],
)
def test_settings_and_factory_reject_silently_unused_camera_calibration(
    tmp_path, provider, backend
):
    from app.core.config import Settings

    path = tmp_path / "camera.json"
    with pytest.raises(ValueError, match="[Cc]alibration requires"):
        Settings(
            _env_file=None,
            vision_provider=provider,
            depth_backend=backend,
            camera_calibration_path=path,
        )
    with pytest.raises(ValueError, match="[Cc]alibration requires"):
        vision.create_provider(provider, depth_backend=backend, camera_calibration_path=path)


@pytest.mark.parametrize("value", [None, ""])
def test_empty_camera_path_keeps_estimated_mode_without_current_directory_default(value):
    from app.core.config import Settings

    settings = Settings(
        _env_file=None,
        vision_provider="urban_objects",
        depth_backend="unidepth_v2",
        camera_calibration_path=value,
    )
    assert settings.camera_calibration_path is None


def test_calibration_environment_path_is_explicit_and_can_be_disabled(monkeypatch, tmp_path):
    from app.core.config import Settings

    path = tmp_path / "camera.json"
    monkeypatch.setenv("LOOKI_CAMERA_CALIBRATION_PATH", str(path))
    settings = Settings(
        _env_file=None, vision_provider="urban_objects", depth_backend="unidepth_v2"
    )
    assert settings.camera_calibration_path == path
    monkeypatch.setenv("LOOKI_CAMERA_CALIBRATION_PATH", "")
    assert Settings(_env_file=None, vision_provider="mock").camera_calibration_path is None
