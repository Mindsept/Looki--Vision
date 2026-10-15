"""Prepare the separate, real Middlebury Bicycle1 structured-light reference."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlretrieve

import numpy as np
from PIL import Image
from prepare_metric_reference import read_calibration, read_pfm, sha256

SOURCE_URL = "https://vision.middlebury.edu/stereo/data/scenes2014/datasets/Bicycle1-perfect/"
FILES = {
    "calib.txt": "d47198449a157672086cf8991eadeb41e365fe4c5fe27000e8a7b1b9caa27421",
    "im0.png": "7eaf9f6a9d8c93d28f9b084cd20cc13de4f41b171e3c37e33547f55f6d0b191b",
    "disp0.pfm": "1e3fb1029de218f70ba8bec40dd33fe74d7c587042d6792a929635cdb5616612",
    "disp0-n.pgm": "4a20be5f3334f15bec2637c959f31c8b31fa2e833082e7a06b674b897cd2fdcf",
}


def prepare(cache: Path, output: Path) -> dict:
    cache.mkdir(parents=True, exist_ok=True)
    original = output / "originals" / "Bicycle1-perfect"
    original.mkdir(parents=True, exist_ok=True)
    for name, expected_hash in FILES.items():
        path = cache / name
        if not path.exists():
            urlretrieve(SOURCE_URL + name, path)
        if sha256(path) != expected_hash:
            raise ValueError(f"Source checksum mismatch: {name}")
        shutil.copyfile(path, original / name)

    calibration = read_calibration(original / "calib.txt")
    calibration["pixel_origin"] = "Rectified camera coordinates from Middlebury calib.txt"
    disparity = read_pfm(original / "disp0.pfm")
    sample_count = np.asarray(Image.open(original / "disp0-n.pgm"))
    denominator = disparity + calibration["disparity_offset_px"]
    valid = np.isfinite(disparity) & (denominator > 0) & (sample_count > 0)
    depth = np.full(disparity.shape, np.nan, dtype=np.float32)
    depth[valid] = calibration["baseline_m"] * calibration["fx_px"] / denominator[valid]
    image = Image.open(original / "im0.png").convert("RGB")
    width, height = image.size
    if disparity.shape != (height, width) or sample_count.shape != (height, width):
        raise ValueError("Image and reference grids differ")
    if (calibration["width"], calibration["height"]) != (width, height):
        raise ValueError("Image and calibration grids differ")
    filename = "Bicycle1-perfect.jpg"
    image.save(output / filename, quality=95, subsampling=0)
    gt_dir = output / "ground-truth"
    gt_dir.mkdir(exist_ok=True)
    depth_path = gt_dir / "Bicycle1-perfect_depth.npy"
    mask_path = gt_dir / "Bicycle1-perfect_valid.npy"
    np.save(depth_path, depth, allow_pickle=False)
    np.save(mask_path, valid, allow_pickle=False)
    valid_depth = depth[valid]
    license_note = (
        "No explicit dataset reuse license found on the official download page. "
        "Public availability is not treated as a grant of unrestricted rights."
    )
    manifest = {
        "schema_version": "1.0",
        "dataset": "Middlebury Stereo 2014 Bicycle1-perfect, separate diagnostic reference",
        "prepared_at": datetime.now(UTC).isoformat(),
        "source_url": SOURCE_URL,
        "documentation_url": "https://vision.middlebury.edu/stereo/data/scenes2014/",
        "acquisition_paper": "https://www.cs.middlebury.edu/~schar/papers/datasets-gcpr2014.pdf",
        "license": None,
        "license_note": license_note,
        "attribution": "Daniel Scharstein et al., Middlebury Stereo Datasets, GCPR 2014",
        "ground_truth_available": True,
        "ground_truth_origin": "Calibrated structured-light acquisition, not model predictions",
        "selection_method": (
            "Single official bicycle scene chosen and inspected before model predictions. "
            "Kept separately from the six frozen outdoor ETH3D scenes."
        ),
        "selection_frozen_before_model_inference": True,
        "depth_convention": "z_depth",
        "near_threshold_m": 1.5,
        "evaluation_range_m": {"min": 0.1, "max": 80.0},
        "alignment": "none: no scale or shift fitting",
        "limitations": [
            "This is a static indoor staged scene, not a street or a glasses camera.",
            "The measured scene has no valid reference pixels at or below 1.5 m.",
            "One scene is insufficient to establish model accuracy or generalization.",
            "Valid structured-light samples include half-occluded regions; this mask is not ETH3D's nonoccluded mask.",
        ],
        "images": [
            {
                "id": "Bicycle1-perfect",
                "filename": filename,
                "label": "Vélo réel avec profondeur de référence",
                "description": "Vélo jaune en intérieur devant un drap, sac derrière la roue avant.",
                "source_url": SOURCE_URL,
                "source_scene": "Bicycle1-perfect",
                "license": None,
                "license_note": license_note,
                "author": "Middlebury Stereo dataset authors (Daniel Scharstein et al.)",
                "dimensions": [width, height],
                "sha256": sha256(output / filename),
                "original_path": str((original / "im0.png").relative_to(output)),
                "original_sha256": sha256(original / "im0.png"),
                "preprocessing": {
                    "resize": False,
                    "crop": False,
                    "generated": False,
                    "mode": "RGB",
                    "jpeg_quality": 95,
                },
                "intrinsics": calibration,
                "ground_truth": {
                    "depth_path": str(depth_path.relative_to(output)),
                    "valid_mask_path": str(mask_path.relative_to(output)),
                    "depth_convention": "z_depth",
                    "units": "meters",
                    "dtype": "float32",
                    "invalid_depth": "NaN",
                    "formula": "z_m = (baseline_mm / 1000) * fx_px / (disparity_px + doffs_px)",
                    "formula_source": "https://vision.middlebury.edu/stereo/data/scenes2014/",
                    "pfm_header_scale": -0.003922,
                    "pfm_scale_rule": "Absolute header scale ignored; values are disparity in pixels",
                    "pfm_format_source": "https://vision.middlebury.edu/stereo/submit3/upload-format.html",
                    "mask_rule": "finite disparity and disparity + doffs > 0 and disp0-n.pgm > 0",
                    "mask_semantics": "Valid structured-light reconstruction with at least one contributing sample",
                    "depth_sha256": sha256(depth_path),
                    "valid_mask_sha256": sha256(mask_path),
                    "source_files": {
                        name: {
                            "path": str((original / name).relative_to(output)),
                            "sha256": digest,
                            "url": SOURCE_URL + name,
                        }
                        for name, digest in FILES.items()
                    },
                    "valid_pixels": int(valid.sum()),
                    "valid_fraction": float(valid.mean()),
                    "min_depth_m": float(valid_depth.min()),
                    "median_depth_m": float(np.median(valid_depth)),
                    "max_depth_m": float(valid_depth.max()),
                    "near_pixels_at_1_5_m": int((valid_depth <= 1.5).sum()),
                },
            }
        ],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "looki-middlebury-bicycle",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "fixtures" / "metric-bicycle-reference",
    )
    args = parser.parse_args()
    manifest = prepare(args.cache_dir.resolve(), args.output_dir.resolve())
    reference = manifest["images"][0]["ground_truth"]
    print(json.dumps(reference, indent=2))


if __name__ == "__main__":
    main()
