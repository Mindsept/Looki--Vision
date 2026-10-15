"""Adapter UniDepth : tests sans téléchargement ni dépendances d'inférence."""

import json
import sys
from contextlib import nullcontext
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.vision import timing, unidepth
from app.vision.calibration import CameraCalibration
from app.vision.unidepth import (
    DEFAULT_UNIDEPTH_MODEL_ID,
    UNIDEPTH_MODEL_REVISION,
    UNIDEPTH_SOURCE_REVISION,
    UniDepthMetricProvider,
)
from app.vision.urban import object_depth


class Tensor:
    def __init__(self, values, clock=None):
        self.values = np.asarray(values)
        self.clock = clock

    def permute(self, *axes):
        return Tensor(self.values.transpose(axes), clock=self.clock)

    def to(self, device):
        assert device == "cpu"
        return self

    def detach(self):
        return self

    def float(self):
        return self

    def cpu(self):
        if self.clock is not None:
            self.clock.now += 0.1
        return self

    def numpy(self):
        return self.values


def torch_fake(clock=None):
    def from_numpy(values):
        if clock is not None:
            clock.now += 0.2
        return Tensor(values)

    return SimpleNamespace(inference_mode=nullcontext, from_numpy=from_numpy)


class Model:
    def __init__(self, depth=None, intrinsics=None, clock=None):
        self.depth = np.full((1, 1, 4, 8), 1.25, dtype=np.float32) if depth is None else depth
        self.intrinsics = (
            np.array([[[10, 0, 4], [0, 12, 2], [0, 0, 1]]], dtype=np.float32)
            if intrinsics is None
            else intrinsics
        )
        self.clock = clock
        self.eval_called = False
        self.float_called = False
        self.last_input = None
        self.last_camera = None

    def to(self, device):
        assert device == "cpu"
        return self

    def float(self):
        self.float_called = True
        return self

    def eval(self):
        self.eval_called = True
        return self

    def infer(self, rgb, camera=None):
        self.last_input = rgb.values
        self.last_camera = None if camera is None else camera.values
        if self.clock is not None:
            self.clock.now += 0.4
        return {
            "depth": Tensor(self.depth, self.clock),
            "intrinsics": Tensor(self.intrinsics, self.clock),
            # This deliberately different radial distance must never replace z.
            "radius": Tensor(np.full((1, 1, 4, 8), 15)),
        }


def make_provider(model=None, **kwargs):
    return UniDepthMetricProvider(model=model or Model(), torch_module=torch_fake(), **kwargs)


def test_uint8_rgb_enters_official_infer_and_metric_z_is_preserved():
    model = Model()
    provider = make_provider(model)
    result = provider.predict_depth(Image.new("RGBA", (8, 4), (20, 30, 40, 12)))
    assert model.eval_called and model.float_called
    assert model.last_input.shape == (3, 4, 8)
    assert model.last_input.dtype == np.uint8
    assert model.last_input[:, 0, 0].tolist() == [20, 30, 40]
    assert result.depth_m.shape == (4, 8)
    assert (result.depth_m == 1.25).all()
    assert object_depth(result.depth_m, (0, 0, 1, 1), provider.max_depth_m) == (1.25, 1.0)
    assert result.metadata["depth_model_id"] == DEFAULT_UNIDEPTH_MODEL_ID
    assert result.metadata["depth_model_revision"] == UNIDEPTH_MODEL_REVISION
    assert result.metadata["depth_source_revision"] == UNIDEPTH_SOURCE_REVISION
    assert result.metadata["depth_intrinsics_source"] == "estimated"
    assert result.metadata["depth_intrinsics"] == [[10, 0, 4], [0, 12, 2], [0, 0, 1]]
    assert result.metadata["depth_horizontal_fov_deg"] == pytest.approx(43.60281897)
    assert result.metadata["depth_resolution_level"] is None
    assert result.metadata["depth_dtype"] == "float32"
    assert "internal" in result.metadata["depth_timing_scope"]
    assert model.last_camera is None


def test_provided_camera_is_scaled_and_passed_to_official_infer_not_applied_to_depth_afterwards():
    calibration = CameraCalibration(fx=1200, fy=1000, cx=780, cy=390, width=1600, height=800)
    model = Model()
    provider = make_provider(model, camera_calibration=calibration)
    result = provider.predict_depth(Image.new("RGB", (8, 4)))
    expected = [[6, 0, 3.9], [0, 5, 1.95], [0, 0, 1]]
    assert model.last_camera.shape == (3, 3)
    assert model.last_camera.dtype == np.float32
    assert model.last_camera == pytest.approx(np.array(expected))
    assert np.asarray(result.metadata["depth_intrinsics"]) == pytest.approx(np.array(expected))
    assert result.metadata["depth_intrinsics_source"] == "provided"
    assert result.metadata["depth_calibration_reference_size"] == [1600, 800]
    assert result.metadata["depth_intrinsics_image_size"] == [8, 4]
    # Like upstream, this fake still returns its own estimated camera. It must
    # not replace the supplied K in metadata or silently rescale metric outputs.
    assert result.metadata["depth_model_estimated_intrinsics"] == [
        [10, 0, 4],
        [0, 12, 2],
        [0, 0, 1],
    ]
    assert (result.depth_m == 1.25).all()


def test_per_image_calibration_overrides_default_without_leaking_to_next_image():
    default = CameraCalibration(fx=6, fy=5, cx=4, cy=2, width=8, height=4)
    override = CameraCalibration(fx=12, fy=10, cx=4, cy=2, width=8, height=4)
    model = Model()
    provider = make_provider(model, camera_calibration=default)
    provider.predict_depth(Image.new("RGB", (8, 4)), camera_calibration=override)
    assert model.last_camera[0, 0] == 12
    provider.predict_depth(Image.new("RGB", (8, 4)))
    assert model.last_camera[0, 0] == 6
    assert provider.camera_calibration is default


def test_wrong_aspect_calibration_fails_before_model_inference():
    model = Model()
    calibration = CameraCalibration(fx=600, fy=600, cx=400, cy=400, width=800, height=800)
    with pytest.raises(ValueError, match="aspect ratio mismatch"):
        make_provider(model).predict_depth(Image.new("RGB", (8, 4)), camera_calibration=calibration)
    assert model.last_input is None


def test_unvalidated_calibration_is_never_implicitly_accepted():
    with pytest.raises(ValueError, match="CameraCalibration instance"):
        make_provider(camera_calibration={"fx": 4})
    with pytest.raises(ValueError, match="CameraCalibration instance"):
        make_provider().predict_depth(Image.new("RGB", (8, 4)), camera_calibration={"fx": 4})


def test_invalid_depth_stays_invalid_and_does_not_become_an_invented_near_obstacle():
    values = np.full((1, 1, 4, 8), np.nan, dtype=np.float32)
    values[0, 0, 0, :4] = [np.inf, -2, 0, 20_000]
    provider = make_provider(Model(depth=values))
    result = provider.predict_depth(Image.new("RGB", (8, 4)))
    assert np.isnan(result.depth_m[1:]).all()
    assert result.depth_m[0, :4].tolist() == [np.inf, -2, 0, 20_000]
    assert object_depth(result.depth_m, (0, 0, 1, 1), provider.max_depth_m) == (None, 0.0)


@pytest.mark.parametrize("shape", [(1, 4, 8), (2, 1, 4, 8), (1, 1, 8, 4), (1, 1, 2, 4)])
def test_wrong_batch_or_unremoved_padding_fails_instead_of_mismapping_boxes(shape):
    provider = make_provider(Model(depth=np.ones(shape)))
    with pytest.raises(ValueError, match="original image size"):
        provider.predict_depth(Image.new("RGB", (8, 4)))


@pytest.mark.parametrize(
    "matrix",
    [
        np.eye(3),
        np.full((1, 3, 3), np.nan),
        [[[0, 0, 4], [0, 10, 2], [0, 0, 1]]],
        [[[10, 0, 4], [0, -1, 2], [0, 0, 1]]],
        [[[10, 0, 4], [0, 10, 2], [0, 0, 0]]],
    ],
)
def test_invalid_intrinsics_fail_explicitly(matrix):
    with pytest.raises(ValueError, match="invalid estimated camera intrinsics"):
        make_provider(Model(intrinsics=np.array(matrix))).predict_depth(Image.new("RGB", (8, 4)))


@pytest.mark.parametrize("level", [-1, 10, 0.5, True, "2"])
def test_invalid_resolution_level_is_rejected(level):
    with pytest.raises(ValueError, match="resolution_level"):
        make_provider(resolution_level=level)


def test_optional_resolution_policy_is_passed_to_official_model():
    model = Model()
    provider = make_provider(model, resolution_level=2)
    assert model.resolution_level == 2
    result = provider.predict_depth(Image.new("RGB", (8, 4)))
    assert result.metadata["depth_resolution_level"] == 2


def test_timings_distinguish_official_infer_from_outer_transfers(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(unidepth, "perf_counter", lambda: clock.now)
    monkeypatch.setattr(timing, "perf_counter", lambda: clock.now)
    provider = UniDepthMetricProvider(model=Model(clock=clock), torch_module=torch_fake(clock))
    result = provider.predict_depth(Image.new("RGB", (8, 4)))
    assert result.timing_ms == pytest.approx(
        {
            "depth_preprocess_ms": 200,
            "depth_inference_ms": 400,
            "depth_postprocess_ms": 200,
            "depth_total_ms": 800,
        }
    )


def fake_load_dependencies(monkeypatch, tmp_path, config, load_error=None):
    (tmp_path / "config.json").write_text(json.dumps(config))
    captured = {}
    hub = ModuleType("huggingface_hub")

    def snapshot(model_id, **kwargs):
        captured.update(model_id=model_id, **kwargs)
        return str(tmp_path)

    hub.snapshot_download = snapshot
    package = ModuleType("unidepth")
    models = ModuleType("unidepth.models")

    def construct(*, config):
        captured["config"] = config
        return Model()

    models.UniDepthV2 = construct
    safetensors = ModuleType("safetensors")
    safetensors_torch = ModuleType("safetensors.torch")

    def load(model, path, strict):
        if load_error:
            raise load_error
        captured["load"] = (path, strict)

    safetensors_torch.load_model = load
    for name, module in (
        ("huggingface_hub", hub),
        ("unidepth", package),
        ("unidepth.models", models),
        ("safetensors", safetensors),
        ("safetensors.torch", safetensors_torch),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return captured


def test_loader_pins_weights_and_only_accepts_safetensors(monkeypatch, tmp_path):
    # Exact official checkpoint semantics: JSON null becomes Python None.
    config = {"model": {"pixel_encoder": {"pretrained": None}}}
    captured = fake_load_dependencies(monkeypatch, tmp_path, config)
    provider = UniDepthMetricProvider(torch_module=torch_fake())
    assert provider.model_revision == UNIDEPTH_MODEL_REVISION
    assert captured["revision"] == UNIDEPTH_MODEL_REVISION
    assert captured["allow_patterns"] == ["config.json", "model.safetensors"]
    assert captured["config"]["model"]["pixel_encoder"]["pretrained"] is None
    assert captured["load"] == (str(tmp_path / "model.safetensors"), True)


def test_model_loading_failure_does_not_fall_back_to_mock_or_pickle(monkeypatch, tmp_path):
    config = {"model": {"pixel_encoder": {"pretrained": None}}}
    fake_load_dependencies(monkeypatch, tmp_path, config, RuntimeError("weights mismatch"))
    with pytest.raises(RuntimeError, match="weights mismatch"):
        UniDepthMetricProvider(torch_module=torch_fake())


@pytest.mark.parametrize("pretrained", ["", False, True, "/tmp/backbone.pth"])
def test_config_cannot_trigger_a_hidden_backbone_download_or_pickle_load(
    monkeypatch, tmp_path, pretrained
):
    captured = fake_load_dependencies(
        monkeypatch, tmp_path, {"model": {"pixel_encoder": {"pretrained": pretrained}}}
    )
    with pytest.raises(ValueError, match="separate pretrained backbone"):
        UniDepthMetricProvider(torch_module=torch_fake())
    assert "load" not in captured


def test_missing_pretrained_flag_is_not_implicitly_accepted(monkeypatch, tmp_path):
    captured = fake_load_dependencies(monkeypatch, tmp_path, {"model": {"pixel_encoder": {}}})
    with pytest.raises(ValueError, match="separate pretrained backbone"):
        UniDepthMetricProvider(torch_module=torch_fake())
    assert "config" not in captured
    assert "load" not in captured
