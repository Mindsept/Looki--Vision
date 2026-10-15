"""Produire des cartes métriques reproductibles pour compare_metric_depth.py.

Exécuter les modèles un par un, sur les mêmes images entières. Aucun usage de
la vérité terrain pendant l'inférence, aucun recalage d'échelle. Les cartes
sont remises à la résolution image avant évaluation (bilinéaire float32).
"""

import argparse
import hashlib
import json
import os
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def relative(path: Path, base: Path) -> str:
    return os.path.relpath(path.resolve(), base.resolve())


def run(args: argparse.Namespace) -> None:
    import torch

    from app.vision.depth_anything import DEFAULT_OUTDOOR_MODEL_ID, DepthAnythingMetricProvider

    torch.set_num_threads(args.threads)
    model_key = args.model + ("_calibrated" if args.calibrated else "")
    if args.model == "depth_anything_v2":
        provider = DepthAnythingMetricProvider(DEFAULT_OUTDOOR_MODEL_ID, args.device)
        label = "Depth Anything V2 Outdoor Small"
    else:
        from app.vision.unidepth import UniDepthMetricProvider

        provider = UniDepthMetricProvider(device=args.device)
        label = "UniDepthV2 ViT-S"
        if args.calibrated:
            label += " + calibration réelle"
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "predictions.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    elif args.reuse_manifest:
        seed = args.reuse_manifest.resolve()
        manifest = json.loads(seed.read_text())
        manifest["samples"] = [s for s in manifest["samples"] if s.get("ground_truth")]
        for sample in manifest["samples"]:
            sample["image_path"] = relative(seed.parent / sample["image_path"], output)
            for key in ("depth_path", "valid_mask_path"):
                sample["ground_truth"][key] = relative(
                    seed.parent / sample["ground_truth"][key], output
                )
            for pred in sample["predictions"].values():
                pred["depth_path"] = relative(seed.parent / pred["depth_path"], output)
    else:
        manifest = {
            "schema_version": "1.0",
            "evaluation": {
                "min_depth_m": 0.1,
                "max_depth_m": 80.0,
                "near_threshold_m": 1.5,
                "depth_convention": "z_depth",
            },
            "models": {},
            "samples": [],
        }
    manifest["models"][model_key] = {
        "label": label,
        "model_id": provider.model_id,
        "revision": provider.model_revision,
        "max_depth_m": provider.max_depth_m,
        "device": args.device,
        "torch_version": torch.__version__,
        "torch_threads": torch.get_num_threads(),
        "platform": platform.platform(),
        "depth_convention": "z_depth",
        "input_camera_calibration": (
            "official dataset camera intrinsics; depth ground truth never passed to model"
            if args.calibrated
            else "none; no ground-truth intrinsics supplied"
        ),
        "output_resize": "bilinear float32 to original image dimensions; no crop",
        "timing_note": "One pass, first sample retained; loading and evaluation resize excluded",
    }
    detections = {}
    if args.detections_results:
        evidence = json.loads(args.detections_results.read_text())
        for sample in evidence["images"]:
            detections.setdefault(sample["filename"], sample["frame"]["detections"])
    destination = output / "predictions" / model_key
    destination.mkdir(parents=True, exist_ok=True)
    for source_path in args.image_manifest:
        source = json.loads(source_path.read_text())
        for item in source["images"]:
            image_path = (source_path.parent / item["filename"]).resolve()
            sample_id = f"{source_path.parent.name}-{Path(item['filename']).stem}"
            image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
            sample = next((s for s in manifest["samples"] if s["id"] == sample_id), None)
            if sample is None:
                sample = {
                    "id": sample_id,
                    "kind": "ground_truth" if item.get("ground_truth") else "urban",
                    "image_path": relative(image_path, output),
                    "image_sha256": image_hash,
                    "source": {**item, "dataset": source.get("dataset", source_path.parent.name)},
                    "predictions": {},
                    "detections": [
                        {key: detection[key] for key in ("label", "confidence", "bbox")}
                        for detection in detections.get(item["filename"], [])
                    ],
                }
                if gt := item.get("ground_truth"):
                    sample["ground_truth"] = {
                        "depth_path": relative(source_path.parent / gt["depth_path"], output),
                        "valid_mask_path": relative(
                            source_path.parent / gt["valid_mask_path"], output
                        ),
                        "unit": "m",
                        "depth_convention": "z_depth",
                    }
                manifest["samples"].append(sample)
            elif sample["image_sha256"] != image_hash:
                raise ValueError(f"Input changed between model runs: {sample_id}")
            with Image.open(image_path) as opened:
                image = opened.convert("RGB")
                if args.calibrated:
                    from app.vision.calibration import CameraCalibration

                    camera = item["intrinsics"]
                    calibration = CameraCalibration.from_dict(
                        {
                            "width": camera["width"],
                            "height": camera["height"],
                            "fx": camera["fx_px"],
                            "fy": camera["fy_px"],
                            "cx": camera["cx_px"],
                            "cy": camera["cy_px"],
                        }
                    )
                    prediction = provider.predict_depth(image, camera_calibration=calibration)
                else:
                    prediction = provider.predict_depth(image)
                depth = np.asarray(prediction.depth_m, dtype=np.float32)
                if depth.shape != (image.height, image.width):
                    depth = torch.nn.functional.interpolate(
                        torch.from_numpy(depth)[None, None],
                        size=(image.height, image.width),
                        mode="bilinear",
                        align_corners=False,
                    )[0, 0].numpy()
            target = destination / f"{sample_id}.npy"
            np.save(target, depth, allow_pickle=False)
            sample["predictions"][model_key] = {
                "depth_path": relative(target, output),
                "unit": "m",
                "depth_convention": "z_depth",
                "timing_ms": prediction.timing_ms,
                "metadata": prediction.metadata,
                "map_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
            manifest["updated_at"] = datetime.now(UTC).isoformat()
            temporary = manifest_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(manifest, indent=2, allow_nan=False))
            temporary.replace(manifest_path)
            print(
                json.dumps(
                    {"model": model_key, "sample": sample_id, "timing_ms": prediction.timing_ms}
                ),
                flush=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("depth_anything_v2", "unidepth_v2"), required=True)
    parser.add_argument("--image-manifest", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--detections-results", type=Path)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--calibrated", action="store_true")
    parser.add_argument("--reuse-manifest", type=Path, help="Reuse saved baselines, GT scenes only")
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads must be positive")
    if args.calibrated and args.model != "unidepth_v2":
        parser.error("--calibrated requires unidepth_v2")
    run(args)


if __name__ == "__main__":
    main()
