#!/usr/bin/env python3
"""Compare saved metric depth maps, without loading models or fitting predictions.

The input manifest contains model metadata and samples with same-shape .npy maps
in metres of camera-axis Z depth. Optional laser ground truth and a boolean valid
mask enable absolute accuracy metrics. A single list of normalized detector boxes
per sample isolates depth changes from detection changes. Paths are relative to
the manifest (absolute paths are also accepted).

Usage from the repository root::

    backend/.venv/bin/python backend/scripts/compare_metric_depth.py \\
        --manifest output/metric-depth-comparison/inference-manifest.json

Definitions follow the official Depth Anything V2 metric implementation:
https://github.com/DepthAnything/Depth-Anything-V2/blob/main/metric_depth/util/metric.py
There is deliberately no median scaling, affine fit, crop, resize, or clipping.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import math
import mimetypes
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "output/metric-depth-comparison"
DEFAULT_EVALUATION = {
    "min_depth_m": 0.1,
    "max_depth_m": 80.0,
    "near_threshold_m": 1.5,
    "depth_convention": "z_depth",
}
FUSION = {"depth_percentile": 20, "central_bbox_ratio": 0.8, "min_valid_fraction": 0.5}
METRIC_KEYS = ("abs_rel", "mae_m", "rmse_m", "delta1")
POLICY = {
    "alignment": "none",
    "prediction_clipping": False,
    "resizing": False,
    "ground_truth_mask": "provided mask AND finite GT AND min_depth_m <= GT <= max_depth_m",
    "primary_support": "fixed GT mask AND finite positive predictions from every compared model",
    "own_support": "fixed GT mask AND finite positive predictions from this model",
    "aggregation": "pixel-weighted micro aggregation, including squared errors before RMSE",
    "out_of_range_predictions": "positive predictions outside evaluation range remain errors",
    "unknown_proximity": "invalid predictions reported separately; never counted as true negatives",
    "near_boundary": "depth <= near_threshold_m",
    "fusion": FUSION,
    "fusion_range": "finite positive depth <= models[key].max_depth_m (evaluation.max_depth_m only if absent); mirrors each runtime provider ceiling, independently from GT metrics",
    "offline_alerts": "expected from fixed detector boxes only; not proof of API/WS alert delivery",
}


def divide(numerator: int | float, denominator: int | float) -> float | None:
    return float(numerator / denominator) if denominator else None


def empty_sums() -> dict[str, int | float]:
    return {
        "pixel_count": 0,
        "absolute_relative_error_sum": 0.0,
        "absolute_error_sum_m": 0.0,
        "squared_error_sum_m2": 0.0,
        "delta1_correct_count": 0,
        "tp": 0,
        "fp": 0,
        "fn": 0,
        "tn": 0,
    }


def metric_sums(gt: np.ndarray, pred: np.ndarray, threshold: float) -> dict[str, int | float]:
    """Sufficient statistics on explicitly selected finite positive pixels."""
    gt, pred = np.asarray(gt, dtype=np.float64), np.asarray(pred, dtype=np.float64)
    if gt.shape != pred.shape:
        raise ValueError("Metric inputs must have the same shape")
    if not (
        np.isfinite(gt).all() and np.isfinite(pred).all() and (gt > 0).all() and (pred > 0).all()
    ):
        raise ValueError("Metric inputs must be finite positive depths")
    if not gt.size:
        return empty_sums()
    error = np.abs(pred - gt)
    truth_near, pred_near = gt <= threshold, pred <= threshold
    return {
        "pixel_count": int(gt.size),
        "absolute_relative_error_sum": float(np.sum(error / gt)),
        "absolute_error_sum_m": float(np.sum(error)),
        "squared_error_sum_m2": float(np.sum(error**2)),
        "delta1_correct_count": int(np.sum(np.maximum(gt / pred, pred / gt) < 1.25)),
        "tp": int(np.sum(truth_near & pred_near)),
        "fp": int(np.sum(~truth_near & pred_near)),
        "fn": int(np.sum(truth_near & ~pred_near)),
        "tn": int(np.sum(~truth_near & ~pred_near)),
    }


def merge_sums(entries: list[dict[str, int | float]]) -> dict[str, int | float]:
    return {key: sum(entry[key] for entry in entries) for key in empty_sums()}


def metrics_from_sums(sums: dict[str, int | float]) -> dict[str, Any]:
    count = sums["pixel_count"]
    tp, fp, fn, tn = (int(sums[key]) for key in ("tp", "fp", "fn", "tn"))
    return {
        "pixel_count": count,
        "abs_rel": divide(sums["absolute_relative_error_sum"], count),
        "mae_m": divide(sums["absolute_error_sum_m"], count),
        "rmse_m": math.sqrt(sums["squared_error_sum_m2"] / count) if count else None,
        "delta1": divide(sums["delta1_correct_count"], count),
        "proximity": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "gt_near_count": tp + fn,
            "gt_non_near_count": tn + fp,
            "precision": divide(tp, tp + fp),
            "recall": divide(tp, tp + fn),
            "f1": divide(2 * tp, 2 * tp + fp + fn) if tp + fn else None,
            "has_positive_ground_truth": bool(tp + fn),
        },
    }


def coverage_counts(
    pred: np.ndarray, mask: np.ndarray, evaluation: dict[str, Any], model: dict[str, Any]
) -> dict[str, Any]:
    values = pred[mask]
    positive = np.isfinite(values) & (values > 0)
    low, high = evaluation["min_depth_m"], evaluation["max_depth_m"]
    result = {
        "reference_pixel_count": int(values.size),
        "valid_prediction_count": int(positive.sum()),
        "invalid_prediction_count": int((~positive).sum()),
        "below_evaluation_range_count": int((positive & (values < low)).sum()),
        "above_evaluation_range_count": int((positive & (values > high)).sum()),
        "within_evaluation_range_count": int((positive & (values >= low) & (values <= high)).sum()),
        "at_or_above_99_percent_eval_max_count": int((positive & (values >= high * 0.99)).sum()),
    }
    declared_max = model.get("max_depth_m", model.get("metadata", {}).get("max_depth_m"))
    if isinstance(declared_max, (int, float)) and math.isfinite(declared_max) and declared_max > 0:
        result["declared_model_max_depth_m"] = declared_max
        result["at_or_above_99_percent_model_max_count"] = int(
            (positive & (values >= declared_max * 0.99)).sum()
        )
    return coverage_fractions(result)


def coverage_fractions(counts: dict[str, Any]) -> dict[str, Any]:
    result = dict(counts)
    total = counts["reference_pixel_count"]
    for key, value in counts.items():
        if key.endswith("_count") and key != "reference_pixel_count":
            result[key.removesuffix("_count") + "_fraction"] = divide(value, total)
    return result


def compare_ground_truth(
    gt: np.ndarray,
    predictions: dict[str, np.ndarray],
    evaluation: dict[str, Any],
    models: dict[str, dict[str, Any]],
    provided_mask: np.ndarray | None = None,
) -> dict[str, Any]:
    """Evaluate shared and own support, with explicit missing-depth coverage."""
    if provided_mask is not None and (
        provided_mask.shape != gt.shape or provided_mask.dtype != bool
    ):
        raise ValueError("Ground-truth valid_mask must be boolean with the depth map shape")
    fixed = np.isfinite(gt) & (gt >= evaluation["min_depth_m"]) & (gt <= evaluation["max_depth_m"])
    if provided_mask is not None:
        fixed &= provided_mask
    if not fixed.any():
        raise ValueError("Ground truth has no valid pixel in the evaluation range")
    own_masks = {}
    common = fixed.copy()
    for key, pred in predictions.items():
        if pred.shape != gt.shape:
            raise ValueError(f"Prediction {key} shape {pred.shape} differs from GT {gt.shape}")
        own_masks[key] = fixed & np.isfinite(pred) & (pred > 0)
        common &= own_masks[key]
    threshold = evaluation["near_threshold_m"]
    result = {
        "total_image_pixels": int(gt.size),
        "gt_valid_pixels": int(fixed.sum()),
        "gt_valid_fraction": float(fixed.mean()),
        "gt_near_pixels": int((fixed & (gt <= threshold)).sum()),
        "common_support_pixels": int(common.sum()),
        "common_support_fraction": float(common.sum() / fixed.sum()),
        "models": {},
    }
    for key, pred in predictions.items():
        own = own_masks[key]
        common_sums = metric_sums(gt[common], pred[common], threshold)
        own_sums = metric_sums(gt[own], pred[own], threshold)
        unknown_near = int((fixed & ~own & (gt <= threshold)).sum())
        unknown_non_near = int((fixed & ~own & (gt > threshold)).sum())
        result["models"][key] = {
            "coverage": coverage_counts(pred, fixed, evaluation, models[key]),
            "common_support": metrics_from_sums(common_sums),
            "own_support": metrics_from_sums(own_sums),
            "common_sums": common_sums,
            "own_sums": own_sums,
            "unknown_prediction_gt_near_count": unknown_near,
            "unknown_prediction_gt_non_near_count": unknown_non_near,
            "near_recall_including_unknown_as_missed": divide(
                own_sums["tp"], own_sums["tp"] + own_sums["fn"] + unknown_near
            ),
        }
    return result


def bbox_depth(depth: np.ndarray, bbox: list[float], max_depth_m: float) -> dict[str, Any]:
    """Same p20/central80%/50%-valid fusion for every model's unchanged box."""
    if len(bbox) != 4 or not all(math.isfinite(value) and 0 <= value <= 1 for value in bbox):
        raise ValueError("Detection bbox must contain four finite normalized coordinates")
    x1, y1, x2, y2 = bbox
    if x1 >= x2 or y1 >= y2:
        raise ValueError("Detection bbox must have positive width and height")
    height, width = depth.shape
    margin_x, margin_y = (x2 - x1) * 0.1, (y2 - y1) * 0.1
    region = depth[
        math.floor((y1 + margin_y) * height + 1e-9) : math.ceil((y2 - margin_y) * height - 1e-9),
        math.floor((x1 + margin_x) * width + 1e-9) : math.ceil((x2 - margin_x) * width - 1e-9),
    ]
    valid = np.isfinite(region) & (region > 0) & (region <= max_depth_m)
    fraction = float(valid.mean()) if region.size else 0.0
    return {
        "distance_m": float(np.percentile(region[valid], 20)) if fraction >= 0.5 else None,
        "depth_valid_fraction": fraction,
        "region_pixel_count": int(region.size),
        "valid_pixel_count": int(valid.sum()),
    }


def evaluate_boxes(
    predictions: dict[str, np.ndarray],
    detections: list[dict[str, Any]],
    evaluation: dict[str, Any],
    models: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    observations = []
    for index, detection in enumerate(detections, 1):
        if not isinstance(detection.get("label"), str) or not detection["label"]:
            raise ValueError("Each detection requires a nonempty label")
        confidence = detection.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError("Each detection requires a finite confidence between zero and one")
        per_model = {}
        for key, pred in predictions.items():
            max_depth = (models or {}).get(key, {}).get("max_depth_m", evaluation["max_depth_m"])
            if (
                not isinstance(max_depth, (int, float))
                or not math.isfinite(max_depth)
                or max_depth <= 0
            ):
                raise ValueError(f"Model {key} max_depth_m must be finite and positive")
            observation = bbox_depth(pred, detection["bbox"], max_depth)
            observation["model_depth_ceiling_m"] = max_depth
            distance = observation["distance_m"]
            observation["near"] = (
                distance <= evaluation["near_threshold_m"] if distance is not None else None
            )
            per_model[key] = observation
        observations.append({"index": index, **detection, "models": per_model})
    return observations


def resolve_path(base: Path, value: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("File paths must be nonempty strings")
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def fingerprint(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
    }


def load_depth(base: Path, item: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    if item.get("unit") != "m" or item.get("depth_convention") != "z_depth":
        raise ValueError(
            "Every depth map must explicitly declare unit='m' and depth_convention='z_depth'"
        )
    path = resolve_path(base, item["depth_path"])
    depth = np.load(path, allow_pickle=False)
    if (
        not isinstance(depth, np.ndarray)
        or depth.ndim != 2
        or not np.issubdtype(depth.dtype, np.floating)
    ):
        raise ValueError("Depth maps must be two-dimensional floating-point .npy arrays")
    if not depth.size:
        raise ValueError("Depth map is empty")
    artifact = {**fingerprint(path), "shape": list(depth.shape), "dtype": str(depth.dtype)}
    if item.get("map_sha256") and item["map_sha256"] != artifact["sha256"]:
        raise ValueError(f"Depth map SHA256 changed: {path.name}")
    return depth.astype(np.float64), artifact


def load_evaluation(manifest: dict[str, Any]) -> dict[str, Any]:
    evaluation = {**DEFAULT_EVALUATION, **manifest.get("evaluation", {})}
    for key in ("min_depth_m", "max_depth_m", "near_threshold_m"):
        if (
            not isinstance(evaluation[key], (int, float))
            or not math.isfinite(evaluation[key])
            or evaluation[key] <= 0
        ):
            raise ValueError(f"evaluation.{key} must be finite and positive")
    if not evaluation["min_depth_m"] <= evaluation["near_threshold_m"] <= evaluation["max_depth_m"]:
        raise ValueError("Evaluation requires min_depth_m <= near_threshold_m <= max_depth_m")
    if evaluation["depth_convention"] != "z_depth":
        raise ValueError("Only explicitly declared camera-axis z_depth is supported")
    return evaluation


def evaluate_sample(
    sample: dict[str, Any], base: Path, models: dict[str, Any], evaluation: dict[str, Any]
) -> dict[str, Any]:
    if set(sample["predictions"]) != set(models):
        raise ValueError("Every sample must contain exactly the same compared model keys")
    image_path = resolve_path(base, sample["image_path"])
    image_evidence = fingerprint(image_path)
    if sample.get("image_sha256") and sample["image_sha256"] != image_evidence["sha256"]:
        raise ValueError(f"Image SHA256 changed: {sample['id']}")
    result = {
        "id": sample["id"],
        "kind": sample.get("kind", "ground_truth" if sample.get("ground_truth") else "urban"),
        "source": sample.get("source", {}),
        "image": image_evidence,
        "predictions": {},
        "ground_truth": None,
        "status": "passed",
    }
    predictions = {}
    for key, prediction in sample["predictions"].items():
        if prediction.get("error"):
            raise ValueError(f"Inference {key} failed: {prediction['error']}")
        depth, artifact = load_depth(base, prediction)
        predictions[key] = depth
        timing = prediction.get("timing_ms", {})
        if not isinstance(timing, dict) or any(
            not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0
            for value in timing.values()
        ):
            raise ValueError(f"Invalid timing_ms for {key}")
        result["predictions"][key] = {
            "artifact": artifact,
            "timing_ms": timing,
            "metadata": prediction.get("metadata", {}),
            "whole_map_coverage": coverage_counts(
                depth, np.ones(depth.shape, dtype=bool), evaluation, models[key]
            ),
        }
    if len({depth.shape for depth in predictions.values()}) != 1:
        raise ValueError("All compared predictions must have exactly the same shape")
    if sample.get("ground_truth"):
        ground_truth = sample["ground_truth"]
        gt, artifact = load_depth(base, ground_truth)
        mask = None
        if ground_truth.get("valid_mask_path"):
            mask_path = resolve_path(base, ground_truth["valid_mask_path"])
            mask = np.load(mask_path, allow_pickle=False)
            artifact["valid_mask"] = fingerprint(mask_path)
        result["ground_truth"] = {
            "artifact": artifact,
            "metadata": {
                key: value
                for key, value in ground_truth.items()
                if key not in {"depth_path", "valid_mask_path"}
            },
            **compare_ground_truth(gt, predictions, evaluation, models, mask),
        }
    result["objects"] = evaluate_boxes(
        predictions, sample.get("detections", []), evaluation, models
    )
    result["offline_expected_alerts"] = {
        key: {
            "near_object_count": sum(
                item["models"][key]["near"] is True for item in result["objects"]
            ),
            "expected_alert": any(
                item["models"][key]["near"] is True for item in result["objects"]
            ),
            "unknown_object_count": sum(
                item["models"][key]["near"] is None for item in result["objects"]
            ),
            "delivery_verified": False,
        }
        for key in models
    }
    return result


def distribution(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "min": None, "max": None, "p50": None, "p95": None}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "p50": float(np.percentile(values, 50)),
        "p95": float(np.percentile(values, 95)),
    }


def summarize(
    samples: list[dict[str, Any]], models: dict[str, Any], *, include_datasets: bool = True
) -> dict[str, Any]:
    passed = [sample for sample in samples if sample["status"] == "passed"]
    ground_truth = [sample["ground_truth"] for sample in passed if sample["ground_truth"]]
    summary = {
        "sample_count": len(samples),
        "passed_count": len(passed),
        "failed_count": len(samples) - len(passed),
        "ground_truth_sample_count": len(ground_truth),
        "no_ground_truth_sample_count": len(passed) - len(ground_truth),
        "gt_valid_pixels": sum(item["gt_valid_pixels"] for item in ground_truth),
        "gt_near_pixels": sum(item["gt_near_pixels"] for item in ground_truth),
        "common_support_pixels": sum(item["common_support_pixels"] for item in ground_truth),
        "models": {},
    }
    for key in models:
        values = [gt["models"][key] for gt in ground_truth]
        common = merge_sums([value["common_sums"] for value in values])
        own = merge_sums([value["own_sums"] for value in values])
        counts = {
            count: sum(value["coverage"][count] for value in values)
            for count in (
                "reference_pixel_count",
                "valid_prediction_count",
                "invalid_prediction_count",
                "below_evaluation_range_count",
                "above_evaluation_range_count",
                "within_evaluation_range_count",
                "at_or_above_99_percent_eval_max_count",
            )
        }
        unknown_near = sum(value["unknown_prediction_gt_near_count"] for value in values)
        unknown_other = sum(value["unknown_prediction_gt_non_near_count"] for value in values)
        timing_keys = sorted(
            {metric for sample in passed for metric in sample["predictions"][key]["timing_ms"]}
        )
        summary["models"][key] = {
            "common_support": metrics_from_sums(common),
            "own_support": metrics_from_sums(own),
            "macro_scene_average": {
                metric: float(np.mean(valid_values))
                if (
                    valid_values := [
                        value["common_support"][metric]
                        for value in values
                        if value["common_support"][metric] is not None
                    ]
                )
                else None
                for metric in ("abs_rel", "mae_m", "delta1")
            },
            "macro_scene_count": sum(
                value["common_support"]["pixel_count"] > 0 for value in values
            ),
            "coverage": coverage_fractions(counts),
            "unknown_prediction_gt_near_count": unknown_near,
            "unknown_prediction_gt_non_near_count": unknown_other,
            "near_recall_including_unknown_as_missed": divide(
                own["tp"], own["tp"] + own["fn"] + unknown_near
            ),
            "latency_ms": {
                metric: distribution(
                    [
                        sample["predictions"][key]["timing_ms"][metric]
                        for sample in passed
                        if metric in sample["predictions"][key]["timing_ms"]
                    ]
                )
                for metric in timing_keys
            },
            "offline_expected_alert_sample_count": sum(
                sample["offline_expected_alerts"][key]["expected_alert"] for sample in passed
            ),
            "offline_near_object_count": sum(
                sample["offline_expected_alerts"][key]["near_object_count"] for sample in passed
            ),
        }
    if include_datasets:
        datasets = {}
        for sample in samples:
            source = sample.get("source", sample.get("input", {}).get("source", {}))
            name = str(source.get("dataset", sample.get("kind") or "unspecified"))
            datasets.setdefault(name, []).append(sample)
        summary["by_dataset"] = {
            name: summarize(group, models, include_datasets=False)
            for name, group in datasets.items()
        }
    return summary


def evaluate_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != "1.0":
        raise ValueError("Expected manifest schema_version '1.0'")
    evaluation = load_evaluation(manifest)
    models = manifest["models"]
    if (
        not isinstance(models, dict)
        or len(models) < 2
        or not all(isinstance(value, dict) for value in models.values())
    ):
        raise ValueError("Comparison requires at least two model metadata objects")
    if not isinstance(manifest.get("samples"), list) or not manifest["samples"]:
        raise ValueError("Manifest must contain at least one sample")
    identifiers = [sample["id"] for sample in manifest["samples"]]
    if any(not isinstance(value, str) or not value for value in identifiers) or len(
        set(identifiers)
    ) != len(identifiers):
        raise ValueError("Sample identifiers must be unique nonempty strings")
    samples = []
    for sample in manifest["samples"]:
        try:
            result = evaluate_sample(sample, manifest_path.parent, models, evaluation)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            result = {
                "id": sample["id"],
                "kind": sample.get("kind"),
                "status": "failed",
                "error": str(exc),
                "input": sample,
            }
        samples.append(result)
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "input_manifest": fingerprint(manifest_path),
        "evaluation": evaluation,
        "policy": POLICY,
        "models": models,
        "samples": samples,
        "summary": summarize(samples, models),
        "limitations": [
            "Comparaison exploratoire sur un petit corpus ; aucune certification de sécurité ni validation physique du dispositif.",
            "Les métriques absolues utilisent uniquement les pixels GT mesurés et valides. Les photos urbaines sans GT ne prouvent pas l'exactitude des mètres.",
            "Le support commun évite de comparer des pixels différents. La couverture propre révèle les prédictions manquantes ; une faible couverture interdit une conclusion globale.",
            "Aucun alignement, redimensionnement ou clipping des prédictions par cet évaluateur. La préparation des cartes et leurs paramètres sont documentés par le producteur.",
            "Z profondeur le long de l'axe optique : différente de la distance euclidienne caméra–point en bord d'image.",
            "Le p20 dans une boîte COCO centrale ne constitue ni une segmentation de l'objet ni une mesure de sa surface la plus proche.",
            "La fusion objet applique le plafond de chaque provider, comme le serveur. Une valeur aberrante mais finie sous ce plafond reste visible ; la plage GT 0,1–80 m ne sert pas à la masquer.",
            "Les alertes de ce rapport sont calculées hors ligne ; leur livraison HTTP/WebSocket doit être testée séparément.",
            "Les latences sont fournies par l'inférence, tous échantillons conservés. Matériel, résolution, focales connues ou estimées et chauffe influencent la comparaison.",
        ],
    }


def escaped(value: Any) -> str:
    return html.escape(str(value), quote=True)


def number(value: Any, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def percent(value: Any) -> str:
    return "—" if value is None else f"{value * 100:.1f} %"


def image_html(sample: dict[str, Any]) -> str:
    path = Path(sample["image"]["path"])
    mime = mimetypes.guess_type(path.name)[0]
    if mime not in {"image/jpeg", "image/png", "image/webp"}:
        return "<p>Format de photo non affichable.</p>"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    boxes = []
    for item in sample["objects"]:
        x1, y1, x2, y2 = item["bbox"]
        boxes.append(
            f'<span class="box" style="left:{x1 * 100}%;top:{y1 * 100}%;width:{(x2 - x1) * 100}%;height:{(y2 - y1) * 100}%">'
            f'<b>{item["index"]}</b><span class="box-detail">{escaped(item["label"])}</span></span>'
        )
    return f'<div class="photo"><img alt="{escaped(sample["id"])}" src="data:{mime};base64,{encoded}">{"".join(boxes)}</div>'


def metric_table(
    entries: dict[str, Any], models: dict[str, Any], support: str = "common_support"
) -> str:
    rows = []
    for key, result in entries.items():
        metrics = result[support]
        proximity = metrics["proximity"]
        rows.append(
            f"<tr><th>{escaped(models[key].get('label', key))}</th>"
            f"<td>{number(metrics['abs_rel'])}</td><td>{number(metrics['mae_m'])}</td><td>{number(metrics['rmse_m'])}</td>"
            f"<td>{percent(metrics['delta1'])}</td><td>{percent(proximity['precision'])}</td>"
            f"<td>{percent(proximity['recall'])}</td><td>{percent(proximity['f1'])}</td><td>{metrics['pixel_count']:,}</td></tr>"
        )
    return (
        '<div class="scroll"><table><thead><tr><th>Modèle</th><th>AbsRel ↓</th><th>MAE m ↓</th><th>RMSE m ↓</th><th>δ₁ ↑</th><th>Précision proche</th><th>Rappel proche</th><th>F1 proche</th><th>Pixels</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


def macro_table(entries: dict[str, Any], models: dict[str, Any]) -> str:
    rows = []
    for key, result in entries.items():
        metrics = result["macro_scene_average"]
        rows.append(
            f"<tr><th>{escaped(models[key].get('label', key))}</th><td>{number(metrics['abs_rel'])}</td>"
            f"<td>{number(metrics['mae_m'])}</td><td>{percent(metrics['delta1'])}</td><td>{result['macro_scene_count']}</td></tr>"
        )
    return (
        '<div class="scroll"><table><thead><tr><th>Modèle</th><th>AbsRel moyen / scène ↓</th><th>MAE moyen / scène m ↓</th><th>δ₁ moyen / scène ↑</th><th>Scènes</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


def render_html(report: dict[str, Any]) -> str:
    summary, models = report["summary"], report["models"]
    threshold = report["evaluation"]["near_threshold_m"]
    coverage_rows = []
    latency_rows = []
    for key, values in summary["models"].items():
        coverage = values["coverage"]
        coverage_rows.append(
            f"<tr><th>{escaped(models[key].get('label', key))}</th><td>{percent(coverage['valid_prediction_fraction'])}</td>"
            f"<td>{coverage['invalid_prediction_count']:,}</td><td>{coverage['above_evaluation_range_count']:,}</td>"
            f"<td>{coverage['at_or_above_99_percent_eval_max_count']:,}</td><td>{values['unknown_prediction_gt_near_count']:,}</td>"
            f"<td>{percent(values['near_recall_including_unknown_as_missed'])}</td></tr>"
        )
        for metric, values_ms in values["latency_ms"].items():
            latency_rows.append(
                f"<tr><th>{escaped(models[key].get('label', key))}</th><td>{escaped(metric)}</td><td>{values_ms['count']}</td><td>{number(values_ms['p50'], 1)}</td><td>{number(values_ms['p95'], 1)}</td><td>{number(values_ms['min'], 1)}–{number(values_ms['max'], 1)}</td></tr>"
            )
    cards = []
    dataset_sections = []
    for name, dataset in summary.get("by_dataset", {}).items():
        if dataset["ground_truth_sample_count"]:
            dataset_sections.append(
                f'<section class="panel"><h3>{escaped(name)}</h3><p>{dataset["ground_truth_sample_count"]} scènes avec GT ; '
                f"{dataset['gt_valid_pixels']:,} pixels GT valides, dont {dataset['gt_near_pixels']:,} proches.</p>"
                "<h4>Chaque scène a le même poids</h4>"
                + macro_table(dataset["models"], models)
                + "<h4>Agrégation pondérée par pixel dans ce jeu de données</h4>"
                + metric_table(dataset["models"], models)
                + "</section>"
            )
    for sample in report["samples"]:
        if sample["status"] != "passed":
            cards.append(
                f'<article class="failed"><h3>{escaped(sample["id"])}</h3><p>{escaped(sample["error"])}</p></article>'
            )
            continue
        truth = sample["ground_truth"]
        note = (
            f"GT disponible : {truth['gt_valid_pixels']:,} pixels valides, dont {truth['gt_near_pixels']:,} à ≤ {threshold:g} m. Support commun : {truth['common_support_pixels']:,}."
            if truth
            else "Sans vérité terrain : ces distances et alertes attendues permettent une comparaison des sorties, aucune conclusion d'exactitude."
        )
        object_rows = []
        for item in sample["objects"]:
            cells = []
            for key in models:
                observation = item["models"][key]
                label = (
                    "PROCHE"
                    if observation["near"]
                    else ("inconnue" if observation["near"] is None else "non proche")
                )
                cells.append(
                    f'<td class="{"near" if observation["near"] else ""}">{number(observation["distance_m"], 2)} m · {label}<small>couverture {percent(observation["depth_valid_fraction"])}</small></td>'
                )
            object_rows.append(
                f"<tr><th>#{item['index']} {escaped(item['label'])}<small>score {percent(item['confidence'])}</small></th>{''.join(cells)}</tr>"
            )
        objects_table = (
            '<div class="scroll"><table><thead><tr><th>Même boîte COCO</th>'
            + "".join(
                f"<th>{escaped(model.get('label', key))}</th>" for key, model in models.items()
            )
            + "</tr></thead><tbody>"
            + "".join(object_rows)
            + "</tbody></table></div>"
            if object_rows
            else "<p>Aucune boîte objet fournie ; aucune alerte objet ne peut être déduite.</p>"
        )
        source = sample["source"]
        source_link = (
            source.get("source_url", source.get("url")) if isinstance(source, dict) else None
        )
        link = (
            f'<a href="{escaped(source_link)}">Source de la photo / GT</a>'
            if isinstance(source_link, str) and source_link.startswith(("https://", "http://"))
            else "Source détaillée dans les preuves JSON."
        )
        cards.append(
            f"<article><h3>{escaped(sample['id'])}</h3><p>{note}</p>{image_html(sample)}"
            + (metric_table(truth["models"], models) if truth else "")
            + objects_table
            + f"<p>{link}</p><details><summary>Paramètres et preuves par modèle</summary><pre>{escaped(json.dumps({'source': source, 'ground_truth_metadata': truth['metadata'] if truth else None, 'predictions': sample['predictions'], 'offline_expected_alerts': sample['offline_expected_alerts']}, indent=2, ensure_ascii=False))}</pre></details></article>"
        )
    limits = "".join(f"<li>{escaped(note)}</li>" for note in report["limitations"])
    return f"""<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LOOKi — Comparaison profondeur métrique</title>
<style>body{{margin:0;background:#f3f6fa;color:#14213b;font:15px/1.55 system-ui,sans-serif}}main{{max-width:1280px;margin:auto;padding:40px}}h1{{font-size:clamp(28px,4vw,44px);line-height:1.15}}h2{{margin-top:36px}}.eyebrow{{letter-spacing:.16em;color:#316d7a;font-weight:750}}.intro,article,.panel{{background:white;border:1px solid #dbe3ec;border-radius:16px;padding:24px;margin:20px 0}}.chips{{display:flex;gap:12px;flex-wrap:wrap}}.chips span{{background:#e7f0f1;padding:8px 14px;border-radius:8px}}.scroll{{overflow-x:auto;margin:16px 0}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{text-align:left;padding:11px;border-bottom:1px solid #dde5ef;vertical-align:top}}thead{{background:#eef3f8}}td{{font-variant-numeric:tabular-nums}}small{{display:block;color:#63738a;font-size:11px}}.near{{background:#fff0de;color:#8a3c00;font-weight:700}}.photo{{position:relative;max-width:960px;margin:20px auto;line-height:0;overflow:hidden}}.photo img{{display:block;width:100%;height:auto}}.box{{position:absolute;border:2px solid #ffcb40;box-sizing:border-box;line-height:1.2}}.box b{{background:#ffcb40;color:#19223a;font-size:12px;padding:1px 3px;position:absolute;left:0;top:0}}.box-detail{{display:none;position:absolute;background:#16243ddd;color:white;padding:4px;white-space:nowrap;left:0;top:16px;font-size:12px}}.box:hover .box-detail{{display:block}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}a{{color:#146478}}.failed{{border-color:#c74c3f}}details{{margin-top:20px}}@media(max-width:700px){{main{{padding:16px}}article,.intro,.panel{{padding:16px}}th,td{{padding:8px}}}}</style>
<main><p class="eyebrow">LOOKi / ÉVALUATION ABSOLUE</p><h1>Comparer les mètres,<br>sans réajuster les prédictions.</h1>
<p>Rapport généré le {escaped(report["generated_at"])}. Modèles évalués sur les mêmes photos et boîtes ; seuil de proximité ≤ {threshold:g} m.</p>
<div class="chips"><span>{summary["passed_count"]}/{summary["sample_count"]} échantillons exploitables</span><span>{summary["ground_truth_sample_count"]} avec GT</span><span>{summary["no_ground_truth_sample_count"]} sans GT</span><span>{summary["gt_near_pixels"]:,} pixels GT proches</span></div>
<section class="intro"><strong>Ce rapport mesure un petit corpus ; il ne certifie pas la fiabilité des lunettes.</strong><p>Les cartes sont comparées en mètres de profondeur Z. Aucun recalage d'échelle, aucun déplacement de seuil, aucun clipping. Les prédictions positives au-delà de {report["evaluation"]["max_depth_m"]:g} m restent des erreurs. Les scènes sans GT ne servent pas à conclure sur la précision physique.</p></section>
<h2>Exactitude par jeu de données</h2><p>Les jeux de données restent séparés : une image haute résolution ne doit pas masquer les résultats des autres scènes. Les moyennes par scène donnent le même poids à chaque image, indépendamment du nombre de pixels GT. δ₁ correspond à un ratio strictement inférieur à 1,25. « — » signifie métrique non définie, notamment le rappel/F1 sans positif GT.</p>{"".join(dataset_sections)}
<h2>Vue globale : deux pondérations explicites</h2><h3>Moyenne par scène, toutes scènes GT confondues</h3>{macro_table(summary["models"], models)}
<h3>Agrégation pondérée par pixel</h3><p>Support commun : {summary["common_support_pixels"]:,} pixels / {summary["gt_valid_pixels"]:,} GT valides. Les grandes cartes et les scènes à forte couverture GT pèsent davantage dans ce tableau.</p>
{metric_table(summary["models"], models)}
<h2>Couverture et prédictions manquantes</h2><div class="scroll"><table><thead><tr><th>Modèle</th><th>Prédiction valide / GT</th><th>Invalide</th><th>&gt; max évaluation</th><th>≥ 99 % max</th><th>GT proche sans prédiction</th><th>Rappel incluant inconnus manqués</th></tr></thead><tbody>{"".join(coverage_rows)}</tbody></table></div>
<details><summary>Métriques sur le support propre de chaque modèle</summary><p>Ces supports peuvent différer : regarder la couverture avant toute comparaison.</p>{metric_table(summary["models"], models, "own_support")}</details>
<h2>Latence d'inférence fournie par le producteur</h2><p>Chaque mesure transmise est conservée, y compris la première. Les temps des modèles doivent être interprétés avec le matériel, la résolution et les paramètres exposés ci-dessous.</p><div class="scroll"><table><thead><tr><th>Modèle</th><th>Étape</th><th>N</th><th>p50 ms</th><th>p95 ms</th><th>Min–max ms</th></tr></thead><tbody>{"".join(latency_rows)}</tbody></table></div>
<h2>Photos, boîtes identiques et distances avant / après</h2><p>Boîtes numérotées partagées entre modèles. Distance = p20 des 80 % centraux avec au moins 50 % de profondeur valide. « PROCHE » correspond à une alerte attendue hors ligne, pas à une livraison WebSocket vérifiée.</p>{"".join(cards)}
<section class="panel"><h2>Limites et provenance</h2><ul>{limits}</ul><details><summary>Configuration complète et empreinte du manifeste</summary><pre>{escaped(json.dumps({"models": models, "evaluation": report["evaluation"], "policy": report["policy"], "input_manifest": report["input_manifest"]}, indent=2, ensure_ascii=False))}</pre></details></section></main></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = evaluate_manifest(args.manifest.resolve())
    except (ValueError, OSError, KeyError, TypeError) as exc:
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "failed",
            "error": str(exc),
        }
        (args.output_dir / "comparison.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        )
        print(f"Évaluation impossible : {exc}", file=sys.stderr)
        return 1
    (args.output_dir / "comparison.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    )
    (args.output_dir / "comparison.html").write_text(render_html(report))
    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    print(f"Rapport : {(args.output_dir / 'comparison.html').resolve()}")
    return int(bool(report["summary"]["failed_count"]))


if __name__ == "__main__":
    raise SystemExit(main())
