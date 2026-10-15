"""Absolute metric evaluation invariants, using analytic maps and no ML model."""

import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_metric_depth.py"
spec = importlib.util.spec_from_file_location("metric_comparison", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
EVAL = module.DEFAULT_EVALUATION.copy()
MODELS = {"baseline": {"label": "Baseline"}, "candidate": {"label": "Candidate"}}


def comparison(gt, baseline, candidate, mask=None):
    return module.compare_ground_truth(
        np.asarray(gt, dtype=float),
        {
            "baseline": np.asarray(baseline, dtype=float),
            "candidate": np.asarray(candidate, dtype=float),
        },
        EVAL,
        MODELS,
        mask,
    )


def test_wrong_metric_scale_is_not_aligned_away():
    gt = np.array([[1.0, 2.0], [4.0, 8.0]])
    result = comparison(gt, gt, gt * 2)
    exact = result["models"]["baseline"]["common_support"]
    wrong = result["models"]["candidate"]["common_support"]
    assert exact["abs_rel"] == 0
    assert exact["delta1"] == 1
    assert wrong["abs_rel"] == 1
    assert wrong["mae_m"] == 3.75
    assert wrong["rmse_m"] == pytest.approx(math.sqrt(85 / 4))
    assert wrong["delta1"] == 0


def test_positive_predictions_outside_eval_range_remain_raw_errors():
    result = comparison([[1.0, 1.0]], [[1.0, 1.0]], [[160.0, 0.05]])
    candidate = result["models"]["candidate"]
    assert candidate["common_support"]["pixel_count"] == 2
    assert candidate["common_support"]["mae_m"] == pytest.approx((159 + 0.95) / 2)
    assert candidate["coverage"]["valid_prediction_fraction"] == 1
    assert candidate["coverage"]["within_evaluation_range_fraction"] == 0
    assert candidate["coverage"]["above_evaluation_range_count"] == 1
    assert candidate["coverage"]["below_evaluation_range_count"] == 1


def test_gt_mask_range_and_missing_predictions_have_explicit_common_support():
    gt = np.array([[0.05, 1.0, 2.0, 81.0, 3.0]])
    baseline = np.array([[1.0, 1.0, 2.0, 4.0, 3.0]])
    candidate = np.array([[1.0, np.nan, 4.0, 4.0, 3.0]])
    mask = np.array([[True, True, True, True, False]])
    result = comparison(gt, baseline, candidate, mask)
    assert result["gt_valid_pixels"] == 2
    assert result["gt_near_pixels"] == 1
    assert result["common_support_pixels"] == 1
    old, new = result["models"]["baseline"], result["models"]["candidate"]
    assert old["common_support"]["pixel_count"] == new["common_support"]["pixel_count"] == 1
    assert old["own_support"]["pixel_count"] == 2
    assert new["coverage"]["valid_prediction_fraction"] == 0.5
    assert new["unknown_prediction_gt_near_count"] == 1
    assert new["near_recall_including_unknown_as_missed"] == 0
    assert new["common_support"]["proximity"]["f1"] is None


def test_proximity_counts_use_unchanged_inclusive_1_point_5_threshold():
    result = comparison([[1.5, 1.0, 2.0, 3.0]], [[1.5, 2.0, 1.0, 3.0]], [[1.5, 2.0, 1.0, 3.0]])
    proximity = result["models"]["baseline"]["common_support"]["proximity"]
    assert (proximity["tp"], proximity["fp"], proximity["fn"], proximity["tn"]) == (1, 1, 1, 1)
    assert proximity["precision"] == proximity["recall"] == proximity["f1"] == 0.5


def test_no_positive_gt_does_not_claim_perfect_recall_or_f1():
    result = comparison([[2.0, 3.0]], [[2.0, 3.0]], [[1.0, 3.0]])
    old = result["models"]["baseline"]["common_support"]["proximity"]
    new = result["models"]["candidate"]["common_support"]["proximity"]
    assert old["precision"] is None
    assert new["precision"] == 0
    for item in (old, new):
        assert item["recall"] is None
        assert item["f1"] is None
        assert not item["has_positive_ground_truth"]


def test_no_predicted_positive_with_positive_gt_has_zero_recall_and_f1():
    result = comparison([[1.0]], [[3.0]], [[3.0]])
    proximity = result["models"]["baseline"]["common_support"]["proximity"]
    assert proximity["precision"] is None
    assert proximity["recall"] == 0
    assert proximity["f1"] == 0


def test_micro_rmse_aggregates_squared_errors_and_pixel_counts():
    first = module.metric_sums(np.array([1.0]), np.array([5.0]), 1.5)
    second = module.metric_sums(np.ones(3), np.ones(3), 1.5)
    result = module.metrics_from_sums(module.merge_sums([first, second]))
    assert result["pixel_count"] == 4
    assert result["mae_m"] == 1
    assert result["rmse_m"] == 2
    assert result["abs_rel"] == 1
    assert result["proximity"]["recall"] == 0.75
    third = module.metric_sums(np.ones(8), np.ones(8), 1.5)
    assert module.metrics_from_sums(module.merge_sums([first, third]))["rmse_m"] == pytest.approx(
        4 / 3
    )


@pytest.mark.parametrize("invalid", [0.0, -1.0, np.nan, np.inf])
def test_missing_depth_is_neither_a_valid_metric_nor_a_true_negative(invalid):
    result = comparison([[1.0, 2.0]], [[1.0, 2.0]], [[invalid, invalid]])
    new = result["models"]["candidate"]
    assert result["common_support_pixels"] == 0
    assert new["common_support"]["abs_rel"] is None
    assert new["common_support"]["proximity"]["tn"] == 0
    assert new["unknown_prediction_gt_near_count"] == 1
    assert new["unknown_prediction_gt_non_near_count"] == 1
    assert new["near_recall_including_unknown_as_missed"] == 0


def test_delta1_boundary_is_strict():
    sums = module.metric_sums(np.array([1.0, 1.0]), np.array([1.25, 1.249]), 1.5)
    assert module.metrics_from_sums(sums)["delta1"] == 0.5


@pytest.mark.parametrize("mask", [np.array([[1]], dtype=np.uint8), np.array([True])])
def test_gt_mask_must_be_boolean_and_same_shape(mask):
    with pytest.raises(ValueError, match="boolean"):
        comparison([[1.0]], [[1.0]], [[1.0]], mask)


def test_empty_gt_and_mismatched_prediction_shape_are_rejected():
    with pytest.raises(ValueError, match="no valid pixel"):
        comparison([[np.nan]], [[1.0]], [[1.0]])
    with pytest.raises(ValueError, match="differs from GT"):
        comparison([[1.0]], [[1.0]], [[1.0, 1.0]])


def test_shared_bbox_fusion_preserves_raw_scale_and_isolates_depth_change():
    old = np.full((10, 10), 8.0)
    new = np.full((10, 10), 0.8)
    detections = [{"label": "bicycle", "confidence": 0.95, "bbox": [0.1, 0.1, 0.9, 0.9]}]
    result = module.evaluate_boxes({"baseline": old, "candidate": new}, detections, EVAL)
    assert result[0]["bbox"] == detections[0]["bbox"]
    assert result[0]["models"]["baseline"]["distance_m"] == 8
    assert result[0]["models"]["candidate"]["distance_m"] == 0.8
    assert result[0]["models"]["candidate"]["near"] is True
    assert result[0]["models"]["baseline"]["near"] is False


def test_fusion_uses_central_80_percent_p20_and_half_valid_minimum():
    depth = np.full((10, 10), 80.0)
    depth[1:9, 1:9] = np.arange(1, 65).reshape(8, 8)
    result = module.bbox_depth(depth, [0, 0, 1, 1], 80)
    assert result["region_pixel_count"] == 64
    assert result["distance_m"] == pytest.approx(13.6)
    depth[1:5, 1:9] = np.nan
    result = module.bbox_depth(depth, [0, 0, 1, 1], 80)
    assert result["depth_valid_fraction"] == 0.5
    assert result["distance_m"] is not None
    depth[5, 1] = np.inf
    result = module.bbox_depth(depth, [0, 0, 1, 1], 80)
    assert result["distance_m"] is None


def test_fusion_keeps_aberrant_long_distance_when_runtime_model_ceiling_allows_it():
    predictions = {"baseline": np.full((10, 10), 154.0), "candidate": np.full((10, 10), 154.0)}
    models = {"baseline": {"max_depth_m": 80}, "candidate": {"max_depth_m": 10_000}}
    detections = [{"label": "car", "confidence": 0.95, "bbox": [0, 0, 1, 1]}]
    result = module.evaluate_boxes(predictions, detections, EVAL, models)[0]["models"]
    assert result["baseline"]["distance_m"] is None
    assert result["baseline"]["depth_valid_fraction"] == 0
    assert result["candidate"]["distance_m"] == 154
    assert result["candidate"]["depth_valid_fraction"] == 1
    assert result["candidate"]["near"] is False
    assert result["candidate"]["model_depth_ceiling_m"] == 10_000
    # The fusion ceiling must not exclude or clip the same value in GT metrics.
    gt_result = module.compare_ground_truth(np.ones((10, 10)), predictions, EVAL, models)
    assert gt_result["models"]["candidate"]["common_support"]["mae_m"] == 153
    assert gt_result["models"]["baseline"]["common_support"]["mae_m"] == 153
    assert gt_result["models"]["candidate"]["coverage"]["above_evaluation_range_count"] == 100


def test_fusion_rejects_invalid_declared_model_ceiling():
    with pytest.raises(ValueError, match="max_depth_m"):
        module.evaluate_boxes(
            {"candidate": np.ones((10, 10))},
            [{"label": "car", "confidence": 0.9, "bbox": [0, 0, 1, 1]}],
            EVAL,
            {"candidate": {"max_depth_m": 0}},
        )


@pytest.mark.parametrize("bbox", [[0, 0, 0, 1], [-1, 0, 1, 1], [0, 0, np.nan, 1]])
def test_invalid_boxes_are_rejected_instead_of_changed(bbox):
    with pytest.raises(ValueError, match="bbox"):
        module.bbox_depth(np.ones((4, 4)), bbox, 80)


@pytest.fixture
def manifest_path(tmp_path):
    Image.new("RGB", (10, 10), "gray").save(tmp_path / "scene.jpg")
    np.save(tmp_path / "gt.npy", np.ones((10, 10)))
    np.save(tmp_path / "old.npy", np.full((10, 10), 2.0))
    np.save(tmp_path / "new.npy", np.ones((10, 10)))
    np.save(tmp_path / "valid.npy", np.ones((10, 10), dtype=bool))
    manifest = {
        "schema_version": "1.0",
        "evaluation": EVAL,
        "models": MODELS,
        "samples": [
            {
                "id": "laser-scene",
                "kind": "ground_truth",
                "image_path": "scene.jpg",
                "source": {
                    "source_url": "https://example.org/laser",
                    "title": "<script>alert(1)</script>",
                },
                "ground_truth": {
                    "depth_path": "gt.npy",
                    "valid_mask_path": "valid.npy",
                    "unit": "m",
                    "depth_convention": "z_depth",
                },
                "predictions": {
                    "baseline": {
                        "depth_path": "old.npy",
                        "unit": "m",
                        "depth_convention": "z_depth",
                        "timing_ms": {"depth_inference_ms": 100},
                    },
                    "candidate": {
                        "depth_path": "new.npy",
                        "unit": "m",
                        "depth_convention": "z_depth",
                        "timing_ms": {"depth_inference_ms": 200},
                    },
                },
                "detections": [{"label": "person", "confidence": 0.9, "bbox": [0, 0, 1, 1]}],
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def test_manifest_end_to_end_preserves_evidence_without_model_loading(manifest_path):
    result = module.evaluate_manifest(manifest_path)
    assert result["summary"]["passed_count"] == 1
    assert result["summary"]["models"]["candidate"]["common_support"]["abs_rel"] == 0
    assert result["summary"]["models"]["baseline"]["common_support"]["abs_rel"] == 1
    assert result["samples"][0]["offline_expected_alerts"]["candidate"]["expected_alert"] is True
    assert (
        result["samples"][0]["offline_expected_alerts"]["candidate"]["delivery_verified"] is False
    )
    assert len(result["samples"][0]["predictions"]["candidate"]["artifact"]["sha256"]) == 64
    assert (
        result["summary"]["models"]["candidate"]["latency_ms"]["depth_inference_ms"]["p50"] == 200
    )
    assert result["policy"]["alignment"] == "none"
    json.dumps(result, allow_nan=False)
    rendered = module.render_html(result)
    assert "data:image/jpeg;base64," in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "<script>" not in rendered
    assert "pas à une livraison WebSocket vérifiée" in rendered


def mutate_manifest(path, mutation):
    document = json.loads(path.read_text())
    mutation(document)
    path.write_text(json.dumps(document))


@pytest.mark.parametrize("field,value", [("unit", "mm"), ("depth_convention", "ray_distance")])
def test_units_and_convention_mismatch_are_saved_as_failures(manifest_path, field, value):
    mutate_manifest(
        manifest_path,
        lambda doc: doc["samples"][0]["predictions"]["candidate"].update({field: value}),
    )
    result = module.evaluate_manifest(manifest_path)
    assert result["summary"]["failed_count"] == 1
    assert result["summary"]["passed_count"] == 0
    assert "unit='m'" in result["samples"][0]["error"]
    assert result["summary"]["models"]["candidate"]["common_support"]["pixel_count"] == 0


def test_missing_model_does_not_silently_change_comparison_cohort(manifest_path):
    mutate_manifest(manifest_path, lambda doc: doc["samples"][0]["predictions"].pop("candidate"))
    result = module.evaluate_manifest(manifest_path)
    assert result["summary"]["failed_count"] == 1
    assert "same compared model keys" in result["samples"][0]["error"]


def test_failed_inference_is_reported_without_fabricated_metrics(manifest_path):
    mutate_manifest(
        manifest_path,
        lambda doc: doc["samples"][0]["predictions"].update(
            {"candidate": {"error": "out of memory"}}
        ),
    )
    result = module.evaluate_manifest(manifest_path)
    assert result["summary"]["failed_count"] == 1
    assert "out of memory" in result["samples"][0]["error"]


def test_no_gt_photo_keeps_box_comparison_but_has_no_accuracy_score(manifest_path):
    mutate_manifest(manifest_path, lambda doc: doc["samples"][0].pop("ground_truth"))
    result = module.evaluate_manifest(manifest_path)
    assert result["summary"]["passed_count"] == 1
    assert result["summary"]["ground_truth_sample_count"] == 0
    assert result["summary"]["no_ground_truth_sample_count"] == 1
    assert result["summary"]["models"]["candidate"]["common_support"]["abs_rel"] is None
    assert result["samples"][0]["objects"][0]["models"]["candidate"]["distance_m"] == 1
    assert "Sans vérité terrain" in module.render_html(result)


def test_duplicate_sample_identifier_is_rejected(manifest_path):
    mutate_manifest(manifest_path, lambda doc: doc["samples"].append(doc["samples"][0]))
    with pytest.raises(ValueError, match="unique"):
        module.evaluate_manifest(manifest_path)


@pytest.mark.parametrize("dtype", [np.int16, object])
def test_depth_file_cannot_be_integer_or_pickled_array(tmp_path, dtype):
    np.save(tmp_path / "depth.npy", np.ones((2, 2), dtype=dtype))
    with pytest.raises(ValueError):
        module.load_depth(
            tmp_path, {"depth_path": "depth.npy", "unit": "m", "depth_convention": "z_depth"}
        )


def test_cli_saves_failure_and_exits_nonzero(manifest_path, tmp_path, monkeypatch):
    mutate_manifest(
        manifest_path,
        lambda doc: doc["samples"][0]["predictions"]["candidate"].update(
            {"depth_path": "missing.npy"}
        ),
    )
    output = tmp_path / "evaluation"
    monkeypatch.setattr(
        "sys.argv", [str(SCRIPT), "--manifest", str(manifest_path), "--output-dir", str(output)]
    )
    assert module.main() == 1
    assert (output / "comparison.json").exists()
    assert (output / "comparison.html").exists()
    assert json.loads((output / "comparison.json").read_text())["summary"]["failed_count"] == 1


def test_per_dataset_and_unweighted_scene_means_prevent_high_resolution_dominance():
    first = comparison([[1.0]], [[5.0]], [[1.0]])
    second = comparison(np.ones((3, 3)), np.ones((3, 3)), np.ones((3, 3)))
    samples = [
        {
            "status": "passed",
            "kind": "ground_truth",
            "source": {"dataset": dataset},
            "ground_truth": truth,
            "predictions": {key: {"timing_ms": {"depth_inference_ms": latency}} for key in MODELS},
            "offline_expected_alerts": {
                key: {"expected_alert": False, "near_object_count": 0} for key in MODELS
            },
        }
        for dataset, truth, latency in (("ETH3D", first, 100), ("Middlebury", second, 900))
    ]
    result = module.summarize(samples, MODELS)
    old = result["models"]["baseline"]
    assert old["common_support"]["abs_rel"] == 0.4
    assert old["macro_scene_average"]["abs_rel"] == 2
    assert old["macro_scene_average"]["mae_m"] == 2
    assert old["macro_scene_average"]["delta1"] == 0.5
    assert old["macro_scene_count"] == 2
    assert result["by_dataset"]["ETH3D"]["models"]["baseline"]["common_support"]["abs_rel"] == 4
    assert (
        result["by_dataset"]["Middlebury"]["models"]["baseline"]["common_support"]["abs_rel"] == 0
    )
    assert (
        result["by_dataset"]["ETH3D"]["models"]["candidate"]["latency_ms"]["depth_inference_ms"][
            "p50"
        ]
        == 100
    )
    assert (
        result["by_dataset"]["Middlebury"]["models"]["candidate"]["latency_ms"][
            "depth_inference_ms"
        ]["p50"]
        == 900
    )


@pytest.mark.parametrize("artifact", ["image", "depth"])
def test_declared_hash_mismatch_rejects_changed_input(manifest_path, artifact):
    def mutation(doc):
        if artifact == "image":
            doc["samples"][0]["image_sha256"] = "0" * 64
        else:
            doc["samples"][0]["predictions"]["candidate"]["map_sha256"] = "0" * 64

    mutate_manifest(manifest_path, mutation)
    report = module.evaluate_manifest(manifest_path)
    assert report["summary"]["failed_count"] == 1
    assert "SHA256 changed" in report["samples"][0]["error"]
