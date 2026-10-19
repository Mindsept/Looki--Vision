from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from app.services.proximity import ProximityService
from app.vision.base import VisionResult
from app.vision.segmentation import unique_instances
from tests.test_perception import enrich, observation, service, vision


def vehicle(label, *, width=0.6, height=0.28, area=0.13, edges=None, confidence=0.9):
    return observation(
        distance=None,
        label=label,
        confidence=confidence,
        bbox=(0.2, 0.6, 0.2 + width, 0.6 + height),
        perception={"mask_area_ratio": area, "mask_edges": edges or [], "polygons": []},
    )


@pytest.mark.parametrize("label", ["car", "bicycle"])
def test_wide_stationary_vehicle_warns_without_person_height_or_depth(label):
    s = service()
    item = vehicle(label)
    first = enrich(s, 0, vision(item)).detections[0]
    assert first.perception["risk"] == "not_flagged"
    current = enrich(s, 0.4, vision(item)).detections[0]
    assert current.perception["risk"] == "possible"
    assert current.perception["visual_profile"] == label
    assert "large_persistent_object" in current.perception["reason_codes"]
    assert current.distance_m is None


@pytest.mark.parametrize("label", ["car", "bicycle"])
def test_width_growth_contributes_to_vehicle_approach_with_constant_height(label):
    s = service()
    for t, width, area in [(0, 0.25, 0.04), (0.4, 0.35, 0.06), (0.8, 0.48, 0.09)]:
        result = enrich(s, t, vision(vehicle(label, width=width, area=area))).detections[0]
    assert result.perception["motion"] == "approaching"
    assert result.perception["risk"] == "possible"
    assert result.perception["evidence"]["size_measure"] == "extent"
    assert "growing_object_mask" in result.perception["reason_codes"]


@pytest.mark.parametrize("label", ["car", "bicycle"])
def test_small_distant_vehicle_does_not_warn_and_unoccluded_shrink_can_recede(label):
    s = service()
    for t, width, area in [(0, 0.18, 0.01), (0.4, 0.14, 0.006), (0.8, 0.10, 0.003)]:
        result = enrich(
            s, t, vision(vehicle(label, width=width, height=0.15, area=area))
        ).detections[0]
    assert result.perception["motion"] == "receding"
    assert result.perception["risk"] == "not_flagged"
    assert not result.perception["alert_emitted"]


@pytest.mark.parametrize("label", ["person", "bicycle", "car"])
def test_weak_detection_cannot_create_an_alert_but_can_briefly_hold_a_known_track(label):
    s = service()
    strong = vehicle(label, height=0.4, edges=["right"])
    if label == "person":
        strong = observation(None, perception={"mask_area_ratio": 0.13, "mask_edges": ["bottom"]})
    weak = replace(strong, confidence=0.3)
    for t in [0, 0.4, 0.8]:
        assert not enrich(s, t, vision(weak)).detections[0].perception["alert_emitted"]
    assert enrich(s, 1.2, vision(strong)).detections[0].perception["alert_emitted"]
    held = enrich(s, 1.6, vision(weak)).detections[0].perception
    assert held["risk"] == "possible" and not held["alert_emitted"]
    assert "recent_proximity_hold" in held["reason_codes"]
    expired = enrich(s, 2.1, vision(weak)).detections[0].perception
    assert expired["risk"] == "not_flagged"


def test_duplicate_masks_are_suppressed_without_merging_adjacent_objects_or_classes():
    a = vehicle("car")
    duplicate = replace(a, confidence=0.4)
    adjacent = replace(a, confidence=0.7)
    bike = replace(a, label="bicycle", confidence=0.6)
    left = np.zeros((20, 20), bool)
    left[:, :10] = True
    right = ~left
    detections, masks = unique_instances([duplicate, a, adjacent, bike], [left, left, right, left])
    assert detections == [a, adjacent, bike]
    assert all(np.array_equal(x, y) for x, y in zip(masks, [left, right, left], strict=True))


def test_warning_names_all_classes_and_directions_without_claiming_metres():
    text = ProximityService.possible_warning_text(
        [
            {"label": "car", "bbox": [0, 0, 0.2, 1]},
            {"label": "bicycle", "bbox": [0.7, 0, 1, 1]},
            {"label": "person", "bbox": [0.4, 0, 0.6, 1]},
        ]
    )
    assert "voiture à gauche" in text and "vélo à droite" in text and "personne devant" in text
    assert "mètre" not in text and "incertaine" in text


@pytest.mark.parametrize("label", ["car", "bicycle"])
def test_vehicle_alert_is_durable_and_replayed_with_class_specific_text(client, label):
    from tests.test_api import create_session, upload, wait_for_event

    runtime = client.app.state.runtime
    runtime.worker.perception = service()
    item = vehicle(label, height=0.4, edges=["right"])
    runtime.vision = type(
        "Provider",
        (),
        {"analyze": lambda self, image: VisionResult("yolo_seg_track", [item], "test", False)},
    )()
    sid = create_session(client)
    fid = upload(
        client, sid, timestamp=(datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    ).json()["frame_id"]
    event = wait_for_event(client, sid, "alert.possible_proximity", frame_id=fid)
    assert event["payload"]["ruleset"] == "urban-visual-v4"
    assert ("voiture" if label == "car" else "vélo") in event["payload"]["text"]
    assert (
        client.get(f"/api/v1/sessions/{sid}/frames/{fid}").json()["detections"]
        == event["payload"]["detections"]
    )
    with client.websocket_connect(f"/api/v1/sessions/{sid}/ws?after={event['id'] - 1}") as ws:
        ws.receive_json()
        assert ws.receive_json() == event


from tests.test_api import client as api_client  # noqa: E402
from tests.test_api import client_factory as api_factory  # noqa: E402

client = api_client
client_factory = api_factory
