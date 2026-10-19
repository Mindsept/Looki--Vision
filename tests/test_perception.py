from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from app.core.config import Settings
from app.services.perception import PerceptionService
from app.vision.base import DetectionResult, VisionResult
from app.vision.segmentation import fuse_segment_depth, mask_depth


class Tracker:
    def update(self, detections):
        return {i: f"{d.label}:{i}" for i, d in enumerate(detections)}


def observation(distance=7, edges=None, **overrides):
    return replace(
        DetectionResult(
            "person",
            0.9,
            (0.2, 0.2, 0.6, 1),
            distance,
            "metric_model",
            "object",
            1,
            {
                "mask_area_ratio": 0.05,
                "mask_edges": ["bottom"] if edges is None else edges,
                "polygons": [],
            },
        ),
        **overrides,
    )


def vision(item=None):
    return VisionResult("yolo_seg_track", [item or observation()], "test", False)


def service():
    return PerceptionService(Settings(_env_file=None), Tracker)


NOW = datetime.now(UTC)


def enrich(s, t, result=None, session="a", frame=None, dimensions=(960, 540)):
    return s.enrich(
        result or vision(), session, frame or str(t), NOW + timedelta(seconds=t), dimensions
    )


def test_mask_excludes_far_background_and_preserves_raw_metres():
    depth = np.full((100, 100), 7.0)
    mask = np.zeros((100, 100), bool)
    mask[30:90, 45:55] = True
    depth[mask] = 0.8
    assert np.percentile(depth, 20) == 7
    assert mask_depth(mask, depth, 100) == pytest.approx((0.8, 1))
    items = fuse_segment_depth(
        masks=[mask],
        geometry=lambda _: {"mask_area_ratio": 0.06},
        boxes=np.array([[0, 0, 100, 100]]),
        scores=np.array([0.9]),
        labels=np.array([0]),
        image_size=(100, 100),
        depth_m=depth,
        max_depth_m=100,
        id2label={0: "person"},
    )
    assert items[0].distance_m == pytest.approx(0.8)


@pytest.mark.parametrize(
    "mask,depth",
    [
        (np.zeros((8, 8)), np.ones((8, 8))),
        (np.ones((8, 8)), np.full((8, 8), np.nan)),
        (np.eye(8), np.ones((8, 8))),
    ],
)
def test_empty_invalid_or_tiny_mask_has_unknown_depth(mask, depth):
    assert mask_depth(mask, depth, 100)[0] is None


def test_possible_alert_keeps_far_raw_depth_and_explains_contradiction():
    s = service()
    first = enrich(s, 0)
    assert first.detections[0].perception["risk"] == "not_flagged"
    result = enrich(s, 0.7)
    d = result.detections[0]
    assert d.distance_m == 7
    assert d.perception["risk"] == "possible"
    assert d.perception["distance_quality"] == "uncertain"
    assert "depth_visual_disagreement" in d.perception["reason_codes"]
    assert d.perception["track_id"] == first.detections[0].perception["track_id"]
    assert enrich(s, 0.7) is result  # Retry cannot invent another observation.
    assert result.detections[0].perception["track_observations"] == 2


@pytest.mark.parametrize(
    "item",
    [
        observation(edges=[]),
        observation(label="chair"),
        observation(confidence=0.3),
        observation(bbox=(0.2, 0.7, 0.3, 1)),
    ],
)
def test_single_cue_or_unsupported_class_does_not_trigger(item):
    s = service()
    enrich(s, 0, vision(item))
    assert enrich(s, 0.7, vision(item)).detections[0].perception["risk"] == "not_flagged"


def test_growth_with_persistence_is_an_independent_visual_cue():
    s = service()
    initial = observation(
        edges=[], bbox=(0.2, 0.3, 0.6, 0.9), perception={"mask_area_ratio": 0.08, "mask_edges": []}
    )
    enrich(s, 0, vision(initial))
    enrich(
        s,
        0.35,
        vision(
            observation(
                edges=[],
                bbox=(0.2, 0.2, 0.6, 0.9),
                perception={"mask_area_ratio": 0.11, "mask_edges": []},
            )
        ),
    )
    current = observation(
        edges=[], bbox=(0.2, 0.1, 0.6, 0.9), perception={"mask_area_ratio": 0.15, "mask_edges": []}
    )
    result = enrich(s, 0.7, vision(current)).detections[0]
    assert result.perception["risk"] == "possible"
    assert "growing_object_mask" in result.perception["reason_codes"]


@pytest.mark.parametrize(
    "distance,risk,quality", [(1, "metric_near", "estimated"), (None, "possible", "unavailable")]
)
def test_near_is_not_duplicated_and_missing_depth_is_not_zero(distance, risk, quality):
    s = service()
    enrich(s, 0, vision(observation(distance)))
    item = enrich(s, 0.7, vision(observation(distance))).detections[0]
    assert item.distance_m == distance
    assert item.perception["risk"] == risk
    assert item.perception["distance_quality"] == quality


@pytest.mark.parametrize(
    "t,session,dimensions",
    [(3, "a", (960, 540)), (-1, "a", (960, 540)), (0.7, "b", (960, 540)), (0.7, "a", (640, 480))],
)
def test_history_resets_on_gap_rewind_new_session_or_dimensions(t, session, dimensions):
    s = service()
    initial = enrich(s, 0).detections[0]
    result = enrich(s, t, session=session, dimensions=dimensions)
    assert result.metadata["tracking_reset"]
    assert result.detections[0].perception["risk"] == "not_flagged"
    assert result.detections[0].perception["track_id"] != initial.perception["track_id"]


def test_temporal_cache_is_bounded():
    s = service()
    for i in range(80):
        enrich(s, 0, session=str(i))
    assert len(s.streams) == 64


def test_possible_event_is_persisted_and_replayed_without_changing_metric_classification(client):
    from tests.test_api import create_session, events, upload, wait_for_event

    runtime = client.app.state.runtime
    runtime.worker.perception = service()
    runtime.vision = type("Provider", (), {"analyze": lambda self, image: vision()})()
    sid = create_session(client)
    upload(client, sid, timestamp=(datetime.now(UTC) - timedelta(seconds=2)).isoformat())
    wait_for_event(client, sid, "frame.analyzed")
    response = upload(client, sid, timestamp=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    event = wait_for_event(client, sid, "alert.possible_proximity")
    detection = event["payload"]["detections"][0]
    assert detection["distance_m"] == 7 and detection["proximity"] == "far"
    assert "nearest_distance_m" not in event["payload"]
    frame = client.get(f"/api/v1/sessions/{sid}/frames/{response.json()['frame_id']}").json()
    assert frame["detections"] == event["payload"]["detections"]
    with client.websocket_connect(f"/api/v1/sessions/{sid}/ws?after={event['id'] - 1}") as ws:
        assert ws.receive_json()["id"] is None
        assert ws.receive_json() == event
    assert not any(e["type"] == "alert.proximity" for e in events(client, sid))


from tests.test_api import client as api_client  # noqa: E402
from tests.test_api import client_factory as api_factory  # noqa: E402

client = api_client
client_factory = api_factory


def test_large_truncated_person_can_warn_without_waiting_for_a_confirmed_track():
    class NoAssociation:
        def update(self, detections):
            return {}

    s = PerceptionService(Settings(_env_file=None), NoAssociation)
    item = observation(perception={"mask_area_ratio": 0.1, "mask_edges": ["bottom"]})
    result = enrich(s, 0, vision(item)).detections[0]
    assert result.perception["risk"] == "possible"
    assert result.perception["track_id"] is None
    assert "strong_mask_at_edge" in result.perception["reason_codes"]
    assert "persistent_track" not in result.perception["reason_codes"]
    assert result.distance_m == 7
