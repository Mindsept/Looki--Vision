"""Invariants of the real-photo evaluation report, without running an ML model."""

import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test_real_images.py"
spec = importlib.util.spec_from_file_location("real_images_evaluator", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def observation(near=False):
    detections = [
        {
            "label": "obstacle_center",
            "bbox": [0.35, 0.2, 0.65, 0.8],
            "distance_m": 0.8 if near else 2.0,
            "distance_source": "metric_model",
            "confidence": 1.0,
            "proximity": "near" if near else "medium",
        },
        {
            "label": "obstacle_left",
            "bbox": [0.05, 0.2, 0.35, 0.8],
            "distance_m": 4.0,
            "distance_source": "metric_model",
            "confidence": 0.8,
            "proximity": "far",
        },
    ]
    frame = {
        "frame_id": "frame-1",
        "status": "completed",
        "provider": "depth_anything_metric",
        "simulated": False,
        "detections": detections,
    }
    events = [
        {
            "id": 1,
            "type": "frame.analyzed",
            "payload": {
                "frame_id": "frame-1",
                "provider": "depth_anything_metric",
                "simulated": False,
                "detections": list(reversed(detections)),
            },
        }
    ]
    if near:
        events.append(
            {
                "id": 2,
                "type": "alert.proximity",
                "payload": {
                    "frame_id": "frame-1",
                    "provider": "depth_anything_metric",
                    "simulated": False,
                    "detections": [detections[0]],
                    "nearest_distance_m": 0.8,
                    "text": "Attention, obstacle proche.",
                },
            }
        )
    return frame, events


@pytest.mark.parametrize("near", [False, True])
def test_report_accepts_real_observations_with_exact_expected_alert_count(near):
    frame, persisted = observation(near)
    assert module.validate_observation(frame, persisted, deepcopy(persisted)) == []


def test_report_rejects_missing_near_alert():
    frame, persisted = observation(True)
    persisted = persisted[:1]
    violations = module.validate_observation(frame, persisted, persisted)
    assert any("attendu 1" in error for error in violations)


def test_report_rejects_alert_without_near_detection():
    frame, persisted = observation(False)
    _, near_events = observation(True)
    persisted.append(near_events[-1])
    violations = module.validate_observation(frame, persisted, persisted)
    assert any("attendu 0" in error for error in violations)


def test_report_rejects_websocket_missing_or_altered_alert():
    frame, persisted = observation(True)
    received = deepcopy(persisted)
    received[-1]["payload"]["text"] = "Une autre alerte"
    violations = module.validate_observation(frame, persisted, received)
    assert any("non reçu à l'identique" in error for error in violations)


def test_report_rejects_mock_results_even_when_alert_contract_is_consistent():
    frame, persisted = observation(True)
    frame["provider"] = "mock"
    frame["simulated"] = True
    assert module.validate_observation(frame, persisted, persisted)


def test_model_failure_is_not_mistaken_for_a_successful_no_alert_result():
    frame = {"frame_id": "frame-1", "status": "failed", "error_code": "inference_failed"}
    events = [{"id": 1, "type": "analysis.failed", "payload": {"frame_id": "frame-1"}}]
    violations = module.validate_observation(frame, events, events)
    assert any("inference_failed" in error for error in violations)


def test_html_escapes_untrusted_source_text_and_embeds_image_without_remote_assets(tmp_path):
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image bytes")
    record = {
        "filename": "photo.jpg",
        "source": {
            "label": "<script>alert(1)</script>",
            "author": "Author <b>name</b>",
            "source_url": "javascript:alert(1)",
            "license": None,
        },
        "image_path": str(image),
        "frame": None,
        "status": "failed",
        "violations": ["Network <error>"],
        "persisted_events": [],
        "received_events": [],
    }
    report = {
        "images": [record],
        "errors": [],
        "started_at": "2026-10-15T10:00:00Z",
        "status": "failed",
    }
    rendered = module.render_html(report)
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "data:image/jpeg;base64," in rendered
    assert 'href="javascript:' not in rendered
    assert "Licence non documentée" in rendered
    assert "<script src=" not in rendered


def test_image_manifest_cannot_read_files_outside_its_directory(tmp_path):
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (tmp_path / "outside.jpg").write_bytes(b"outside")
    with pytest.raises(ValueError, match="hors dossier"):
        module.load_image(images_dir, "../outside.jpg")


def test_html_has_a_readable_author_fallback_for_missing_photo_attribution(tmp_path):
    record = {
        "filename": "photo.jpg",
        "source": {"label": "Photo", "author": None, "license": None},
        "image_path": str(tmp_path / "photo.jpg"),
        "frame": None,
        "status": "failed",
        "violations": [],
        "persisted_events": [],
        "received_events": [],
    }
    report = {
        "images": [record],
        "errors": [],
        "started_at": "2026-10-15T10:00:00Z",
        "status": "failed",
    }
    rendered = module.render_html(report)
    assert "Auteur non renseigné" in rendered
    assert "· null ·" not in rendered


def urban_observation(near=False):
    frame, events = observation(near)
    frame["provider"] = "urban_objects"
    frame["timing_ms"] = {key: 10.0 for key in module.URBAN_TIMING_KEYS}
    frame["metadata"] = {
        "depth_model_id": "depth-model",
        "object_model_id": "object-model",
        "depth_percentile": 20,
        "central_bbox_ratio": 0.8,
        "confidence_semantics": "object_detection_score",
        "depth_valid_fraction_semantics": "valid_depth_pixel_fraction",
    }
    for index, detection in enumerate(frame["detections"]):
        detection.update(
            label="person",
            observation_type="object",
            confidence=0.67,
            depth_valid_fraction=0.93 if index == 0 else 0.81,
        )
    for event in events:
        event["payload"]["provider"] = "urban_objects"
        if event["type"] == "frame.analyzed":
            event["payload"]["timing_ms"] = deepcopy(frame["timing_ms"])
            event["payload"]["metadata"] = deepcopy(frame["metadata"])
    return frame, events


@pytest.mark.parametrize("near", [False, True])
def test_urban_report_validates_separate_depth_and_detector_results(near):
    frame, events = urban_observation(near)
    assert module.validate_observation(frame, events, deepcopy(events), "urban_objects") == []


def test_urban_report_detects_missing_isolated_depth_timing():
    frame, events = urban_observation()
    del frame["timing_ms"]["depth_inference_ms"]
    violations = module.validate_observation(frame, events, events, "urban_objects")
    assert any("Chronométrages urbains manquants" in error for error in violations)


def test_urban_report_detects_changed_timing_between_http_and_websocket():
    frame, events = urban_observation()
    events[0]["payload"]["timing_ms"]["detection_inference_ms"] = 999
    violations = module.validate_observation(frame, events, events, "urban_objects")
    assert any("timing_ms diffère" in error for error in violations)


def test_urban_report_rejects_distance_without_enough_valid_depth_pixels():
    frame, events = urban_observation()
    frame["detections"][0]["depth_valid_fraction"] = 0.2
    violations = module.validate_observation(frame, events, events, "urban_objects")
    assert any("couverture profondeur suffisante" in error for error in violations)


def test_urban_report_preserves_unknown_object_distance_without_inventing_alert():
    frame, events = urban_observation()
    frame["detections"][0].update(distance_m=None, depth_valid_fraction=0.2, proximity="unknown")
    assert module.validate_observation(frame, events, events, "urban_objects") == []


def test_urban_report_refuses_fixed_regions_instead_of_objects():
    frame, events = urban_observation()
    frame["detections"][0]["observation_type"] = "region"
    violations = module.validate_observation(frame, events, events, "urban_objects")
    assert any("des objets, pas des régions fixes" in error for error in violations)


def test_object_overlay_preserves_repeated_classes_and_separates_score_from_coverage():
    frame, _ = urban_observation()
    overlay, table = module.render_detections(frame, urban=True)
    assert overlay.count('class="roi ') == 2
    assert "person #1" in overlay and "person #2" in overlay
    assert "67.00 %" in table  # Detector score.
    assert "93.00 %" in table  # Valid-depth coverage, never relabeled as confidence.
    assert "Gauche" not in table
    assert "Score détection" in table


def test_old_region_reports_still_show_coverage_from_confidence_when_new_field_is_null():
    frame, _ = observation()
    frame["detections"][0]["depth_valid_fraction"] = None
    _, table = module.render_detections(frame, urban=False)
    assert "100.00 %" in table
    assert "Gauche" in table and "Centre" in table and "Droite" in table


def test_benchmark_keeps_the_slow_first_sample_and_reports_linear_percentiles():
    records = [
        {
            "filename": "scene.jpg",
            "status": "passed",
            "sample_index": index,
            "is_first_sample": index == 1,
            "timing_ms": {"depth_inference_ms": value},
        }
        for index, value in enumerate([1000.0, 100.0, 100.0], start=1)
    ]
    stats = module.build_benchmark({"images": records, "repeat_count": 3, "planned_samples": 3})
    depth = stats["metrics"]["depth_inference_ms"]
    assert stats["first_sample_retained"] is True
    assert depth == {
        "count": 3,
        "p50_ms": 100.0,
        "p95_ms": pytest.approx(910.0),
        "min_ms": 100.0,
        "max_ms": 1000.0,
    }
    assert stats["by_image"]["scene.jpg"]["sample_count"] == 3


def test_benchmark_does_not_hide_failed_samples_or_fabricate_missing_times():
    report = {
        "images": [
            {"filename": "scene.jpg", "status": "passed", "timing_ms": {"depth_inference_ms": 80}},
            {"filename": "scene.jpg", "status": "failed", "timing_ms": {}},
        ],
        "repeat_count": 2,
        "planned_samples": 2,
    }
    stats = module.build_benchmark(report)
    assert stats["failed_samples"] == 1
    assert stats["sample_count"] == 2
    assert stats["metrics"]["depth_inference_ms"]["count"] == 1


def test_urban_html_groups_repeats_but_keeps_all_trial_metrics_and_no_fixed_roi_text(tmp_path):
    frame, events = urban_observation()
    first = {
        "filename": "photo.jpg",
        "source": {"label": "Rue"},
        "image_path": str(tmp_path / "photo.jpg"),
        "frame": frame,
        "status": "passed",
        "violations": [],
        "persisted_events": events,
        "received_events": [{"event": event} for event in events],
        "sample_index": 1,
        "repeat_index": 1,
        "is_first_sample": True,
        "timing_ms": frame["timing_ms"],
        "http_to_ws_analysis_ms": 123.0,
    }
    second = deepcopy(first)
    second.update(
        sample_index=2, repeat_index=2, is_first_sample=False, http_to_ws_analysis_ms=234.0
    )
    report = {
        "images": [first, second],
        "errors": [],
        "expected_provider": "urban_objects",
        "started_at": "2026-10-15T10:00:00Z",
        "status": "passed",
        "repeat_count": 2,
    }
    rendered = module.render_html(report)
    assert rendered.count('<article class="card">') == 1
    assert "Trois zones d’échantillonnage" not in rendered
    assert "123.00" in rendered and "234.00" in rendered
    assert "Inférence profondeur seule" in rendered
    assert "Inférence détecteur seule" in rendered
    assert "p95 (ms)" in rendered
    assert "Score détection" in rendered


def test_dense_urban_scene_keeps_every_box_and_uses_focusable_numbered_labels():
    frame, _ = urban_observation()
    template = frame["detections"][0]
    frame["detections"] = [dict(template, distance_m=float(index + 1)) for index in range(10)]
    overlay, table = module.render_detections(frame, urban=True)
    assert overlay.count('class="roi ') == 10
    assert overlay.count('class="bbox-number"') == 10
    assert overlay.count('tabindex="0"') == 10
    assert "person #10" in table


def test_object_labels_at_the_right_edge_anchor_inside_the_image():
    frame, _ = urban_observation()
    frame["detections"][0]["bbox"] = [0.8, 0.2, 0.99, 0.8]
    overlay, _ = module.render_detections(frame, urban=True)
    assert "align-right" in overlay
