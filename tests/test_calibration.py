"""Contrat de calibration rectifiée et redimensionnement des intrinsèques."""

import json

import numpy as np
import pytest

from app.vision.calibration import CameraCalibration


def payload(**changes):
    return {"fx": 900, "fy": 920, "cx": 620, "cy": 340, "width": 1280, "height": 720, **changes}


def test_json_reference_and_resized_intrinsics_preserve_off_center_principal_point(tmp_path):
    path = tmp_path / "camera.json"
    path.write_text(json.dumps(payload()))
    calibration = CameraCalibration.from_json(path)
    np.testing.assert_allclose(
        calibration.matrix_for_size(640, 360), [[450, 0, 310], [0, 460, 170], [0, 0, 1]]
    )
    np.testing.assert_allclose(
        calibration.matrix_for_size(1280, 720), [[900, 0, 620], [0, 920, 340], [0, 0, 1]]
    )
    assert calibration.cx == 620  # matrix creation never mutates reference K.


def test_integer_rounding_in_full_frame_resize_is_allowed_but_warping_is_not():
    calibration = CameraCalibration.from_dict(payload(width=2964, height=1988))
    matrix = calibration.matrix_for_size(1280, 859)
    assert matrix[0, 0] == pytest.approx(900 * 1280 / 2964)
    assert matrix[1, 1] == pytest.approx(920 * 859 / 1988)
    with pytest.raises(ValueError, match="aspect ratio mismatch"):
        calibration.matrix_for_size(1280, 850)
    with pytest.raises(ValueError, match="aspect ratio mismatch"):
        calibration.matrix_for_size(640, 640)


@pytest.mark.parametrize(
    "field,value",
    [
        ("fx", 0),
        ("fy", -1),
        ("fx", float("nan")),
        ("fy", float("inf")),
        ("cx", float("nan")),
        ("cy", float("-inf")),
        ("fx", "900"),
        ("cx", True),
        ("width", 0),
        ("height", -2),
        ("width", 1280.0),
        ("height", True),
    ],
)
def test_invalid_numeric_parameters_are_rejected(field, value):
    with pytest.raises(ValueError, match="Camera calibration"):
        CameraCalibration.from_dict(payload(**{field: value}))


@pytest.mark.parametrize("field", ["distortion", "extrinsics", "camera_model", "crop"])
def test_unsupported_geometry_cannot_be_silently_discarded(field):
    with pytest.raises(ValueError, match="exactly"):
        CameraCalibration.from_dict(payload(**{field: []}))


@pytest.mark.parametrize("value", [[], None, {}, {"width": 1280, "height": 720}])
def test_incomplete_or_wrong_json_type_is_rejected(value):
    with pytest.raises(ValueError, match="exactly"):
        CameraCalibration.from_dict(value)


def test_duplicate_json_keys_are_rejected(tmp_path):
    path = tmp_path / "camera.json"
    path.write_text('{"fx": 900, "fx": 90, "fy":920,"cx":620,"cy":340,"width":1280,"height":720}')
    with pytest.raises(ValueError, match="Duplicate"):
        CameraCalibration.from_json(path)


@pytest.mark.parametrize("size", [(0, 360), (640, -1), (640.0, 360), (640, True)])
def test_invalid_target_dimensions_are_rejected(size):
    with pytest.raises(ValueError, match="positive integer"):
        CameraCalibration.from_dict(payload()).matrix_for_size(*size)


@pytest.mark.parametrize("focal", [1e300, 1e-300])
def test_unrepresentable_intrinsics_cannot_reach_float32_model(focal):
    with pytest.raises(ValueError, match="float32"):
        CameraCalibration.from_dict(payload(fx=focal)).matrix_for_size(1280, 720)
