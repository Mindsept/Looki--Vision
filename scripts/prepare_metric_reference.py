"""Prepare a fixed real ETH3D subset with laser-derived metric ground truth.

No model is called. Source images and calibration are preserved at native size.
Requires NumPy, Pillow and bsdtar (or 7z/7zz) for the official 7z archives.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlretrieve

import numpy as np
from PIL import Image, ImageDraw

ARCHIVES = {
    "two_view_training.7z": "cb7683a01ba037759f30b5a4f561d302453e20f1f7095cbdc51c8564b218224b",
    "two_view_training_gt.7z": "e440f7ec4444dfd2af33a44495f325a3aed06762432e2419277596ba7dcb9b40",
}
SCENES = {
    "playground_3l": "Aire de jeux extérieure : poteaux, cordes et sol proches.",
    "forest_1s": "Sous-bois extérieur : branches fines et végétation proches.",
    "facade_1s": "Façade extérieure : murs et fenêtres à plusieurs distances.",
    "terrace_2s": "Terrasse extérieure : tables, bancs et façade.",
    "electro_1l": "Équipement technique extérieur : porte, conduites et façade.",
    "playground_1l": "Aire de jeux extérieure : grand équipement et bâtiments.",
}
BASE_URL = "https://www.eth3d.net/data/"
LICENSE_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_pfm(path: Path) -> np.ndarray:
    with path.open("rb") as handle:
        if handle.readline().strip() != b"Pf":
            raise ValueError("Expected a single-channel PFM")
        width, height = map(int, handle.readline().split())
        scale = float(handle.readline())
        if not np.isfinite(scale) or scale == 0:
            raise ValueError("Invalid PFM byte-order marker")
        # Middlebury disparities are stored directly in pixels. The absolute
        # header scale is ignored, per its official upload-format documentation.
        values = np.fromfile(handle, dtype="<f4" if scale < 0 else ">f4")
    if values.size != width * height:
        raise ValueError("PFM payload has an unexpected size")
    return np.flipud(values.reshape(height, width)).astype(np.float32)


def read_calibration(path: Path) -> dict:
    values = dict(line.split("=", 1) for line in path.read_text().splitlines())
    matrix = [
        [float(value) for value in row.split()] for row in values["cam0"].strip("[]").split(";")
    ]
    return {
        "camera_model": "PINHOLE",
        "matrix": matrix,
        "fx_px": matrix[0][0],
        "fy_px": matrix[1][1],
        "cx_px": matrix[0][2],
        "cy_px": matrix[1][2],
        "baseline_m": float(values["baseline"]) / 1000,
        "disparity_offset_px": float(values["doffs"]),
        "width": int(values["width"]),
        "height": int(values["height"]),
        "pixel_origin": "top-left image corner, per ETH3D documentation",
    }


def obtain_sources(cache: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    for name, expected_hash in ARCHIVES.items():
        archive = cache / name
        if not archive.exists():
            urlretrieve(BASE_URL + name, archive)
        if sha256(archive) != expected_hash:
            raise ValueError(f"Source checksum mismatch: {name}")
        if shutil.which("bsdtar"):
            command = ["bsdtar", "-xf", str(archive), "-C", str(cache)]
        else:
            extractor = shutil.which("7z") or shutil.which("7zz")
            if not extractor:
                raise RuntimeError("Install bsdtar, 7z or 7zz to extract sources")
            command = [extractor, "x", "-y", f"-o{cache}", str(archive)]
        subprocess.run(command, check=True, capture_output=True)


def prepare(cache: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "dataset": "ETH3D low-resolution two-view stereo training subset",
        "prepared_at": datetime.now(UTC).isoformat(),
        "source_url": "https://www.eth3d.net/datasets",
        "documentation_url": "https://www.eth3d.net/documentation",
        "license": "CC-BY-NC-SA-4.0",
        "license_url": LICENSE_URL,
        "attribution": "Thomas Schöps et al., ETH3D, CVPR 2017",
        "ground_truth_available": True,
        "ground_truth_origin": "Registered laser-scanner measurements, not model predictions",
        "selection_method": (
            "Six outdoor scenes inspected visually before predictions. Two scenes include "
            "valid laser-reference pixels at or below 1.5 m; four provide negatives. "
            "Selection uses source imagery and reference depth only, never model output."
        ),
        "selection_frozen_before_model_inference": True,
        "depth_convention": "z_depth",
        "near_threshold_m": 1.5,
        "evaluation_range_m": {"min": 0.1, "max": 80.0},
        "alignment": "none: no scale or shift fitting",
        "archives": [
            {"url": BASE_URL + name, "sha256": digest} for name, digest in ARCHIVES.items()
        ],
        "limitations": [
            "Monochrome real images; not representative of the final RGB glasses camera.",
            "Close foreground surfaces include branches, poles and ropes outside COCO classes.",
            "Only pixels with laser ground truth visible in both stereo views are evaluated.",
            "This is a selected diagnostic subset, not the full ETH3D benchmark.",
            "Outdoor playground views share a location and are not independent environments.",
        ],
        "images": [],
    }
    for scene, description in SCENES.items():
        source = cache / scene
        original = output / "originals" / scene
        original.mkdir(parents=True, exist_ok=True)
        for name in (
            "im0.png",
            "im1.png",
            "calib.txt",
            "cameras.txt",
            "images.txt",
            "disp0GT.pfm",
            "mask0nocc.png",
        ):
            shutil.copyfile(source / name, original / name)
        calibration = read_calibration(original / "calib.txt")
        disparity = read_pfm(original / "disp0GT.pfm")
        source_mask = np.asarray(Image.open(original / "mask0nocc.png"))
        if source_mask.ndim == 3:
            source_mask = source_mask[..., 0]
        denominator = disparity + calibration["disparity_offset_px"]
        valid = (source_mask == 255) & np.isfinite(disparity) & (denominator > 0)
        depth = np.full(disparity.shape, np.nan, dtype=np.float32)
        depth[valid] = calibration["baseline_m"] * calibration["fx_px"] / denominator[valid]
        image = Image.open(original / "im0.png").convert("RGB")
        width, height = image.size
        if disparity.shape != (height, width):
            raise ValueError(f"Image/depth shape mismatch: {scene}")
        if (calibration["width"], calibration["height"]) != (width, height):
            raise ValueError(f"Image/calibration shape mismatch: {scene}")
        filename = scene + ".jpg"
        image.save(output / filename, quality=95, subsampling=0)
        gt_dir = output / "ground-truth"
        gt_dir.mkdir(exist_ok=True)
        depth_path = gt_dir / (scene + "_depth.npy")
        mask_path = gt_dir / (scene + "_valid.npy")
        np.save(depth_path, depth, allow_pickle=False)
        np.save(mask_path, valid, allow_pickle=False)
        reference_values = depth[valid]
        manifest["images"].append(
            {
                "id": scene,
                "filename": filename,
                "label": scene,
                "description": description,
                "source_url": "https://www.eth3d.net/datasets",
                "source_scene": scene,
                "license": "CC-BY-NC-SA-4.0",
                "license_url": LICENSE_URL,
                "author": "ETH3D dataset authors (Thomas Schöps et al.)",
                "dimensions": [width, height],
                "sha256": sha256(output / filename),
                "original_path": str((original / "im0.png").relative_to(output)),
                "original_sha256": sha256(original / "im0.png"),
                "preprocessing": {
                    "resize": False,
                    "crop": False,
                    "generated": False,
                    "mode": "grayscale source replicated as RGB",
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
                    "mask_rule": "mask0nocc == 255 and finite disparity and disparity + doffs > 0",
                    "mask_semantics": "Only valid laser-reference pixels observed in both views",
                    "depth_sha256": sha256(depth_path),
                    "valid_mask_sha256": sha256(mask_path),
                    "source_files": {
                        name: {
                            "path": str((original / name).relative_to(output)),
                            "sha256": sha256(original / name),
                        }
                        for name in ("disp0GT.pfm", "mask0nocc.png", "calib.txt")
                    },
                    "valid_pixels": int(valid.sum()),
                    "valid_fraction": float(valid.mean()),
                    "min_depth_m": float(reference_values.min()),
                    "median_depth_m": float(np.median(reference_values)),
                    "max_depth_m": float(reference_values.max()),
                    "near_pixels_at_1_5_m": int((reference_values <= 1.5).sum()),
                },
            }
        )
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    sheet = Image.new("RGB", (960, 520), "white")
    draw = ImageDraw.Draw(sheet)
    for index, item in enumerate(manifest["images"]):
        image = Image.open(output / item["filename"])
        image.thumbnail((320, 220))
        x, y = (index % 3) * 320, (index // 3) * 260
        draw.text((x + 5, y + 5), item["id"], fill="black")
        sheet.paste(image, (x, y + 25))
    sheet.save(output / "contact-sheet.jpg", quality=95)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "looki-eth3d-reference",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "fixtures" / "metric-reference",
    )
    args = parser.parse_args()
    obtain_sources(args.cache_dir.resolve())
    manifest = prepare(args.cache_dir.resolve(), args.output_dir.resolve())
    for item in manifest["images"]:
        gt = item["ground_truth"]
        print(f"{item['id']}: valid={gt['valid_pixels']}, near={gt['near_pixels_at_1_5_m']}")


if __name__ == "__main__":
    main()
