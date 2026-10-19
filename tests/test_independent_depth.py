import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from app.services.perception import PerceptionService, visual_trend
from app.vision.base import DetectionResult, VisionResult
from app.vision.depth_anything import DepthPrediction
from app.vision.masks import pack_masks, unpack_masks
from tests.test_api import client_factory as api_factory
from tests.test_api import create_session, events, upload, wait_for_event
from tests.test_perception import Tracker, enrich, observation, service, vision

client_factory = api_factory


@pytest.mark.parametrize("count", [0, 1, 3])
def test_packed_masks_are_lossless_and_preserve_instance_order(count):
    masks = np.random.default_rng(42).random((count, 17, 23)) > 0.5
    assert np.array_equal(unpack_masks(pack_masks(masks, (23, 17))), masks)


def samples(rate, *, partial=False):
    return [
        {
            "timestamp": t,
            "height": 0.3 * np.exp(rate * t),
            "area": 0.05 * np.exp(2 * rate * t),
            "partial": partial,
        }
        for t in [0, 0.17, 0.41, 0.63, 0.91]
    ]


def test_trend_uses_elapsed_time_instead_of_per_frame_growth():
    motion, h, a = visual_trend(samples(0.3))
    assert motion == "approaching"
    assert h == pytest.approx(0.3) and a == pytest.approx(0.6)
    assert visual_trend(samples(0.3)[::2])[0] == "approaching"
    assert visual_trend(samples(0))[0] == "stable"
    assert visual_trend(samples(-0.3))[0] == "receding"
    assert visual_trend(samples(-0.3, partial=True))[0] == "unknown"


def test_single_size_jump_or_insufficient_time_cannot_establish_a_trend():
    assert visual_trend(samples(0.3)[:2])[0] == "unknown"
    quick = [{**s, "timestamp": s["timestamp"] / 10} for s in samples(0.3)]
    assert visual_trend(quick)[0] == "unknown"


def test_risk_holds_after_mask_shrinks_but_expires_and_does_not_spam_events():
    s = service()
    large = observation(None, perception={"mask_area_ratio": 0.12, "mask_edges": ["bottom"]})
    initial = enrich(s, 0, vision(large)).detections[0].perception
    assert initial["risk"] == "possible" and initial["alert_emitted"]
    partial = observation(
        None,
        bbox=(0.2, 0.7, 0.5, 1),
        perception={"mask_area_ratio": 0.025, "mask_edges": ["bottom"]},
    )
    held = enrich(s, 0.3, vision(partial)).detections[0].perception
    assert held["risk"] == "possible" and not held["alert_emitted"]
    assert "recent_proximity_hold" in held["reason_codes"]
    assert held["motion"] != "receding"
    assert enrich(s, 1.0, vision(partial)).detections[0].perception["risk"] == "not_flagged"


def test_large_stationary_person_can_warn_without_touching_a_border():
    s = service()
    item = observation(None, edges=[], perception={"mask_area_ratio": 0.12, "mask_edges": []})
    enrich(s, 0, vision(item))
    data = enrich(s, 0.4, vision(item)).detections[0].perception
    assert data["risk"] == "possible" and "large_persistent_object" in data["reason_codes"]


class SplitProvider:
    depth_model_id = "test-depth"

    def __init__(self, *, fail=False):
        self.started, self.release = threading.Event(), threading.Event()
        self.fail = fail
        self.depth = SimpleNamespace(predict_depth=self.predict_depth, max_depth_m=100)

    def analyze_visual(self, image, *, include_masks):
        item = DetectionResult(
            "person",
            0.9,
            (0.2, 0.2, 0.8, 1),
            None,
            "metric_model",
            "object",
            None,
            {"mask_area_ratio": 0.12, "mask_edges": ["bottom"], "polygons": []},
        )
        mask = np.ones((image.height, image.width), bool)
        return VisionResult(
            "yolo_seg_track",
            [item],
            "Visual only",
            False,
            {"total_vision_ms": 1},
            {"depth_mode": "independent"},
            pack_masks([mask], image.size) if include_masks else None,
        )

    def predict_depth(self, image):
        self.started.set()
        if not self.release.wait(4):
            raise TimeoutError("Test did not release the depth worker")
        if self.fail:
            raise ValueError("Depth unavailable")
        return DepthPrediction(np.full((image.height, image.width), 0.8), {"depth_total_ms": 1}, {})


@pytest.mark.parametrize("fail", [False, True])
def test_slow_or_failed_depth_does_not_block_visual_results_and_never_leaks_old_metres(
    client_factory, monkeypatch, fail
):
    import app.main

    provider = SplitProvider(fail=fail)
    monkeypatch.setattr(app.main, "create_provider", lambda *args, **kwargs: provider)
    client = client_factory(
        vision_provider="yolo_seg_track", depth_interval_seconds=60, depth_max_age_seconds=10
    )
    runtime = client.app.state.runtime
    runtime.worker.perception = PerceptionService(runtime.settings, Tracker)
    sid = create_session(client)
    first = upload(client, sid).json()["frame_id"]
    try:
        visual = wait_for_event(client, sid, "frame.analyzed", frame_id=first)
        assert provider.started.wait(1)
        assert visual["payload"]["metadata"]["depth_sample_status"] == "scheduled"
        second = upload(client, sid).json()["frame_id"]
        second_visual = wait_for_event(client, sid, "frame.analyzed", frame_id=second)
        assert second_visual["payload"]["detections"][0]["distance_m"] is None
        assert not any(e["type"] == "frame.depth_analyzed" for e in events(client, sid))
    finally:
        provider.release.set()
    if fail:
        wait_for_event(client, sid, "depth.failed", frame_id=first)
        assert client.get(f"/api/v1/sessions/{sid}/frames/{first}").json()["status"] == "completed"
        assert not any(e["type"] == "alert.proximity" for e in events(client, sid))
    else:
        depth = wait_for_event(client, sid, "frame.depth_analyzed", frame_id=first)
        alert = wait_for_event(client, sid, "alert.proximity", frame_id=first)
        assert depth["payload"]["captured_at"] == visual["payload"]["captured_at"]
        assert alert["payload"]["nearest_distance_m"] == pytest.approx(0.8)
        assert depth["payload"]["detections"] == alert["payload"]["detections"]
        assert (
            client.get(f"/api/v1/sessions/{sid}/frames/{first}/depth").json()["payload"]
            == depth["payload"]
        )
        assert (
            client.get(f"/api/v1/sessions/{sid}/frames/{second}/depth").json()["status"]
            == "not_sampled"
        )
        # The original visual result remains immutable after depth arrives.
        assert (
            client.get(f"/api/v1/sessions/{sid}/frames/{first}").json()["detections"]
            == visual["payload"]["detections"]
        )
        with client.websocket_connect(f"/api/v1/sessions/{sid}/ws?after={depth['id'] - 1}") as ws:
            assert ws.receive_json()["id"] is None
            assert ws.receive_json() == depth
            assert ws.receive_json() == alert


def test_stale_depth_is_failed_without_a_metric_warning(client_factory, monkeypatch):
    import app.main

    provider = SplitProvider()
    provider.release.set()
    monkeypatch.setattr(app.main, "create_provider", lambda *args, **kwargs: provider)
    client = client_factory(vision_provider="yolo_seg_track", depth_max_age_seconds=0.5)
    runtime = client.app.state.runtime
    runtime.worker.perception = PerceptionService(runtime.settings, Tracker)
    sid = create_session(client)
    upload(client, sid, timestamp=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    event = wait_for_event(client, sid, "depth.failed")
    assert event["payload"]["code"] == "stale_depth"
    assert not provider.started.is_set()
    assert not any(e["type"] == "alert.proximity" for e in events(client, sid))


def test_depth_recovery_keeps_source_masks_and_publishes_only_one_result(client_factory, monkeypatch):
    from uuid import UUID

    import app.main
    from app.repositories.depth import DepthRepository
    from app.repositories.frames import FrameRepository

    provider = SplitProvider()
    provider.release.set()
    monkeypatch.setattr(app.main, "create_provider", lambda *args, **kwargs: provider)
    client = client_factory(vision_provider="yolo_seg_track", worker_enabled=False)
    runtime = client.app.state.runtime
    runtime.worker.perception = PerceptionService(runtime.settings, Tracker)
    sid = create_session(client)
    fid = upload(client, sid).json()["frame_id"]

    async def interrupt_and_recover():
        async with runtime.db.sessions() as db, db.begin():
            frame = await FrameRepository(db).claim_next()
        await runtime.worker.process(frame)
        async with runtime.db.sessions() as db, db.begin():
            interrupted = await DepthRepository(db).claim_next()
        assert interrupted.status == "processing"
        await runtime.depth_worker.recover()
        assert runtime.depth_worker.busy and not runtime.depth_worker.due
        async with runtime.db.sessions() as db, db.begin():
            recovered = await DepthRepository(db).claim_next()
        assert recovered.id == interrupted.id
        assert recovered.masks == interrupted.masks
        assert recovered.observations == interrupted.observations
        await runtime.depth_worker.process(recovered)
        # A repeated completion cannot emit another result or warning.
        await runtime.depth_worker.process(recovered)
        async with runtime.db.sessions() as db:
            job = await DepthRepository(db).get_for_frame(UUID(sid), UUID(fid))
            assert job.status == "completed"
            assert job.result["frame_id"] == fid

    client.portal.call(interrupt_and_recover)
    recorded = events(client, sid)
    assert sum(e["type"] == "frame.depth_analyzed" for e in recorded) == 1
    assert sum(e["type"] == "alert.proximity" for e in recorded) == 1
    another = create_session(client)
    assert client.get(f"/api/v1/sessions/{another}/frames/{fid}/depth").status_code == 404
