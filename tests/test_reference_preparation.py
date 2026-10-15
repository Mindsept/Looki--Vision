"""Protect metric-reference parsing from unit and vertical-orientation mistakes."""

import importlib.util
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_metric_reference.py"
SPEC = importlib.util.spec_from_file_location("reference_preparation", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


@pytest.mark.parametrize("marker,endian", [(-1, "<"), (1, ">"), (-0.003922, "<")])
def test_pfm_preserves_pixel_disparities_and_image_orientation(tmp_path, marker, endian):
    source = tmp_path / "disparity.pfm"
    # PFM stores the bottom row first. The last marker is used by Bicycle1.
    source.write_bytes(
        f"Pf\n2 2\n{marker}\n".encode()
        + np.array([30.0, np.inf, 10.0, 20.0], dtype=f"{endian}f4").tobytes()
    )
    np.testing.assert_array_equal(
        module.read_pfm(source), np.array([[10, 20], [30, np.inf]], dtype=np.float32)
    )


def test_camera_baseline_is_converted_once_and_offset_is_retained(tmp_path):
    source = tmp_path / "calib.txt"
    source.write_text(
        "cam0=[1000 0 300; 0 900 200; 0 0 1]\n"
        "cam1=[1000 0 310; 0 900 200; 0 0 1]\n"
        "doffs=10\nbaseline=60\nwidth=640\nheight=480\n"
    )
    calibration = module.read_calibration(source)
    assert calibration["baseline_m"] == pytest.approx(0.06)
    assert calibration["disparity_offset_px"] == 10
    assert calibration["matrix"] == [[1000, 0, 300], [0, 900, 200], [0, 0, 1]]
    # A 30 px disparity plus 10 px offset gives a known axial depth of 1.5 m.
    reference_depth = (
        calibration["baseline_m"] * calibration["fx_px"] / (30 + calibration["disparity_offset_px"])
    )
    assert reference_depth == pytest.approx(1.5)


def test_truncated_pfm_is_rejected(tmp_path):
    source = tmp_path / "disparity.pfm"
    source.write_bytes(b"Pf\n2 2\n-1\n" + np.array([1], dtype="<f4").tobytes())
    with pytest.raises(ValueError, match="unexpected size"):
        module.read_pfm(source)
