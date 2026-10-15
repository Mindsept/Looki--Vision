"""Executable contracts for the Pi, iPhone and asynchronous analysis worker."""

from __future__ import annotations

import json
import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "images"
API_KEY = "integration-test-key"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture
def client_factory(tmp_path):
    """Use a real file-backed database and the real worker for each application."""
    with ExitStack() as stack:

        def make_client(**overrides):
            config = {
                "database_url": f"sqlite+aiosqlite:///{tmp_path / (str(uuid4()) + '.sqlite')}",
                "api_key": API_KEY,
                "create_schema": True,
                "vision_provider": "mock",
                "centres_provider": "mock",
                "worker_poll_seconds": 0.01,
                "websocket_poll_seconds": 0.01,
                "fixtures_dir": FIXTURE_DIR.parent,
            }
            config.update(overrides)
            app = create_app(Settings(_env_file=None, **config))
            return stack.enter_context(TestClient(app, headers=HEADERS))

        yield make_client


@pytest.fixture
def client(client_factory):
    return client_factory()


def create_session(client: TestClient) -> str:
    response = client.post(
        "/api/v1/sessions", json={"center_id": "demo-centre", "user_id": "test-user"}
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "active"
    return response.json()["id"]


def upload(
    client: TestClient,
    session_id: str,
    scene: str = "near",
    *,
    frame_id: str | None = None,
    timestamp: str | None = None,
    image: bytes | None = None,
    media_type: str = "image/jpeg",
):
    return client.post(
        f"/api/v1/sessions/{session_id}/frames",
        data={
            "frame_id": frame_id or str(uuid4()),
            "timestamp": timestamp or datetime.now(UTC).isoformat(),
        },
        files={
            "image": (
                f"{scene}.jpg",
                image if image is not None else (FIXTURE_DIR / f"{scene}.jpg").read_bytes(),
                media_type,
            )
        },
    )


def events(client: TestClient, session_id: str, *, after: int = 0) -> list[dict]:
    response = client.get(
        f"/api/v1/sessions/{session_id}/events", params={"after": after, "limit": 100}
    )
    assert response.status_code == 200, response.text
    return response.json()


def wait_for_event(client: TestClient, session_id: str, event_type: str, **payload):
    deadline = time.monotonic() + 8
    observed = []
    while time.monotonic() < deadline:
        observed = events(client, session_id)
        for event in observed:
            if event["type"] == event_type and all(
                event["payload"].get(key) == value for key, value in payload.items()
            ):
                return event
        time.sleep(0.02)
    pytest.fail(f"Missing {event_type} {payload}; received: {observed}")


def receive_event(websocket, event_type: str, **payload):
    # Callers first wait for the durable event over HTTP, so receive cannot race
    # an analysis failure. The cap detects an unexpected stream of events.
    for _ in range(30):
        event = websocket.receive_json()
        if event["type"] == event_type and all(
            event["payload"].get(key) == value for key, value in payload.items()
        ):
            return event
    pytest.fail(f"WebSocket did not deliver {event_type}")


def test_near_frame_is_analyzed_alerted_and_replayed_over_websocket(client):
    session_id = create_session(client)
    frame_id = str(uuid4())
    with client.websocket_connect(f"/api/v1/sessions/{session_id}/ws?after=0") as ws:
        initial = ws.receive_json()
        assert initial["type"] == "session.state"
        assert initial["id"] is None
        response = upload(client, session_id, frame_id=frame_id)
        assert response.status_code == 202, response.text
        assert response.json()["frame_id"] == frame_id
        alert = wait_for_event(client, session_id, "alert.proximity", frame_id=frame_id)
        pushed = receive_event(ws, "alert.proximity", frame_id=frame_id)
        assert pushed == alert
        assert pushed["payload"]["text"]

    frame = client.get(f"/api/v1/sessions/{session_id}/frames/{frame_id}")
    assert frame.status_code == 200
    result = frame.json()
    assert result["status"] == "completed"
    assert result["provider"] == "mock"
    assert result["simulated"] is True
    assert result["description"]
    assert result["latency_ms"] >= 0
    assert any(item["proximity"] == "near" for item in result["detections"])

    with client.websocket_connect(
        f"/api/v1/sessions/{session_id}/ws?after={alert['id'] - 1}"
    ) as ws:
        replayed = receive_event(ws, "alert.proximity", frame_id=frame_id)
        assert replayed == alert


@pytest.mark.parametrize(
    "distance,confidence,expected_alert",
    [(1.2, 0.9, True), (1.5, 0.9, True), (1.51, 0.9, False), (None, 0.9, False), (1.2, 0.4, False)],
)
def test_object_distance_warning_and_stage_timings_persist_over_http_and_ws(
    client, distance, confidence, expected_alert
):
    from app.vision.base import DetectionResult, VisionResult

    class ObjectProvider:
        def analyze(self, image):
            return VisionResult(
                provider="urban_objects",
                detections=[
                    DetectionResult(
                        label="person",
                        confidence=confidence,
                        bbox=(0.01, 0.1, 0.3, 0.9),
                        distance_m=distance,
                        distance_source="metric_model",
                        observation_type="object",
                        depth_valid_fraction=0.8,
                    )
                ],
                description="Personne détectée.",
                simulated=False,
                timing_ms={"depth_inference_ms": 125.0, "detection_inference_ms": 80.0},
                metadata={"depth_model_id": "test-depth", "object_model_id": "test-detector"},
            )

    client.app.state.runtime.vision = ObjectProvider()
    session_id = create_session(client)
    frame_id = str(uuid4())
    with client.websocket_connect(f"/api/v1/sessions/{session_id}/ws") as ws:
        assert upload(client, session_id, frame_id=frame_id).status_code == 202
        analyzed = wait_for_event(client, session_id, "frame.analyzed", frame_id=frame_id)
        assert receive_event(ws, "frame.analyzed", frame_id=frame_id) == analyzed
        frame = client.get(f"/api/v1/sessions/{session_id}/frames/{frame_id}").json()
        assert frame["timing_ms"] == analyzed["payload"]["timing_ms"]
        assert frame["metadata"] == analyzed["payload"]["metadata"]
        assert frame["timing_ms"]["depth_inference_ms"] == 125.0
        assert frame["detections"][0]["observation_type"] == "object"
        assert frame["detections"][0]["depth_valid_fraction"] == 0.8
        assert frame["detections"][0]["confidence"] == confidence
        alerts = [e for e in events(client, session_id) if e["type"] == "alert.proximity"]
        assert len(alerts) == int(expected_alert)
        if expected_alert:
            assert receive_event(ws, "alert.proximity", frame_id=frame_id) == alerts[0]
            assert "personne à gauche" in alerts[0]["payload"]["text"]
            assert alerts[0]["payload"]["nearest_distance_m"] == distance


@pytest.mark.parametrize(
    "distance,expected_proximity", [(1.49, "near"), (1.50, "near"), (1.51, "medium")]
)
def test_calibrated_metric_boundary_flows_through_worker_database_and_single_ws(
    client_factory, tmp_path, monkeypatch, distance, expected_proximity
):
    """A deterministic model stub tests orchestration, not physical distance accuracy."""
    import app.main as main_module
    from app.vision.base import DetectionResult, VisionResult

    matrix = [[510.0, 0.0, 320.0], [0.0, 505.0, 240.0], [0.0, 0.0, 1.0]]
    calibration_path = tmp_path / "camera.json"
    calibration_path.write_text(
        json.dumps({"width": 640, "height": 480, "fx": 510, "fy": 505, "cx": 320, "cy": 240})
    )
    captured = {}

    class CalibratedModelStub:
        def analyze(self, image):
            assert image.size == (640, 480)
            return VisionResult(
                provider="urban_objects",
                simulated=False,
                detections=[
                    DetectionResult(
                        label="bicycle",
                        confidence=0.95,
                        bbox=(0.35, 0.2, 0.65, 0.9),
                        distance_m=distance,
                        distance_source="metric_model",
                        observation_type="object",
                        depth_valid_fraction=0.9,
                    )
                ],
                description="Prédiction déterministe injectée pour tester le contrat logiciel.",
                timing_ms={"depth_inference_ms": 12.0, "detection_inference_ms": 4.0},
                metadata={
                    "depth_backend": "unidepth_v2",
                    "depth_model_id": "test/unidepth-calibrated",
                    "depth_intrinsics_source": "provided",
                    "depth_intrinsics": matrix,
                    "depth_calibration_reference_size": [640, 480],
                    "depth_intrinsics_image_size": [640, 480],
                    "depth_semantics": "camera z in meters; not Euclidean range",
                    "test_model_stub": True,
                },
            )

    def fake_factory(*args, **kwargs):
        captured.update(kwargs)
        return CalibratedModelStub()

    monkeypatch.setattr(main_module, "create_provider", fake_factory)
    client = client_factory(
        vision_provider="urban_objects",
        depth_backend="unidepth_v2",
        camera_calibration_path=calibration_path,
    )
    assert captured["camera_calibration_path"] == calibration_path
    assert captured["depth_backend"] == "unidepth_v2"
    session_id = create_session(client)
    frame_id = str(uuid4())
    with client.websocket_connect(f"/api/v1/sessions/{session_id}/ws") as ws:
        assert ws.receive_json()["type"] == "session.state"
        assert upload(client, session_id, scene="clear", frame_id=frame_id).status_code == 202
        persisted_analysis = wait_for_event(client, session_id, "frame.analyzed", frame_id=frame_id)
        assert receive_event(ws, "frame.analyzed", frame_id=frame_id) == persisted_analysis
        frame = client.get(f"/api/v1/sessions/{session_id}/frames/{frame_id}").json()
        assert frame["status"] == "completed"
        assert frame["metadata"] == persisted_analysis["payload"]["metadata"]
        assert frame["metadata"]["depth_intrinsics_source"] == "provided"
        assert frame["metadata"]["depth_intrinsics"] == matrix
        assert frame["metadata"]["near_threshold_m"] == 1.5
        assert frame["metadata"]["test_model_stub"] is True
        detection = frame["detections"][0]
        assert detection["distance_m"] == distance
        assert detection["distance_source"] == "metric_model"
        assert detection["proximity"] == expected_proximity
        durable = events(client, session_id)
        alerts = [event for event in durable if event["type"] == "alert.proximity"]
        assert len(alerts) == int(expected_proximity == "near")
        if alerts:
            alert = receive_event(ws, "alert.proximity", frame_id=frame_id)
            assert alert == alerts[0]
            assert alert["payload"]["nearest_distance_m"] == distance
            assert alert["payload"]["threshold_m"] == 1.5
            assert alert["payload"]["detections"][0]["distance_source"] == "metric_model"
            assert (
                alert["payload"]["text"]
                == f"Attention, vélo devant, à environ {distance:.2f} mètre."
            )
        ws.send_json({"type": "ping"})
        assert receive_event(ws, "pong")["id"] is None


def test_every_near_frame_emits_an_alert_and_retry_is_idempotent(client):
    session_id = create_session(client)
    frame_ids = [str(uuid4()), str(uuid4())]
    timestamp = datetime.now(UTC).isoformat()
    for frame_id in frame_ids:
        response = upload(client, session_id, frame_id=frame_id, timestamp=timestamp)
        assert response.status_code == 202, response.text
        wait_for_event(client, session_id, "alert.proximity", frame_id=frame_id)
    before_retry = events(client, session_id)
    retry = upload(client, session_id, frame_id=frame_ids[0], timestamp=timestamp)
    assert retry.status_code == 200, retry.text
    assert events(client, session_id) == before_retry
    alerts = [event for event in before_retry if event["type"] == "alert.proximity"]
    assert {event["payload"]["frame_id"] for event in alerts} == set(frame_ids)
    assert len(alerts) == 2


@pytest.mark.parametrize("scene,expected_proximity", [("far", "far"), ("clear", None)])
def test_far_or_clear_frame_does_not_emit_proximity_alert(client, scene, expected_proximity):
    session_id = create_session(client)
    frame_id = str(uuid4())
    response = upload(client, session_id, scene, frame_id=frame_id)
    assert response.status_code == 202, response.text
    wait_for_event(client, session_id, "frame.analyzed", frame_id=frame_id)
    frame = client.get(f"/api/v1/sessions/{session_id}/frames/{frame_id}").json()
    if expected_proximity:
        assert any(item["proximity"] == expected_proximity for item in frame["detections"])
    else:
        assert frame["detections"] == []
    assert not any(event["type"] == "alert.proximity" for event in events(client, session_id))


def test_question_and_answer_share_the_same_websocket_and_request_is_idempotent(client):
    session_id = create_session(client)
    request_id = str(uuid4())
    question = {"type": "question", "request_id": request_id, "text": "Que vois-tu ?"}
    with client.websocket_connect(f"/api/v1/sessions/{session_id}/ws") as ws:
        assert ws.receive_json()["type"] == "session.state"
        ws.send_json(question)
        persisted = wait_for_event(client, session_id, "answer", request_id=request_id)
        answer = receive_event(ws, "answer", request_id=request_id)
        assert answer == persisted
        assert answer["payload"]["text"]
        ws.send_json(question)
        ws.send_json({"type": "ping"})
        assert receive_event(ws, "pong")["id"] is None
    answers = [event for event in events(client, session_id) if event["type"] == "answer"]
    assert len(answers) == 1


def test_authentication_required_for_rest_and_websocket(client):
    session_id = create_session(client)
    assert (
        client.get(
            f"/api/v1/sessions/{session_id}", headers={"Authorization": "Bearer incorrect"}
        ).status_code
        == 401
    )
    # Empty explicit header overrides the authenticated client's default.
    assert (
        client.post("/api/v1/sessions", json={}, headers={"Authorization": ""}).status_code == 401
    )
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/api/v1/sessions/{session_id}/ws", headers={"Authorization": "Bearer incorrect"}
        ):
            pytest.fail("Unauthorized WebSocket was accepted")


def test_same_frame_identifier_cannot_be_reused_for_different_content(client):
    session_id = create_session(client)
    frame_id = str(uuid4())
    timestamp = datetime.now(UTC).isoformat()
    first = upload(client, session_id, "near", frame_id=frame_id, timestamp=timestamp)
    assert first.status_code == 202
    conflict = upload(client, session_id, "far", frame_id=frame_id, timestamp=timestamp)
    assert conflict.status_code == 409, conflict.text


def test_worker_queue_is_bounded_but_retries_remain_allowed(client_factory):
    client = client_factory(worker_enabled=False, max_pending_frames=1)
    session_id = create_session(client)
    frame_id = str(uuid4())
    timestamp = datetime.now(UTC).isoformat()
    assert upload(client, session_id, frame_id=frame_id, timestamp=timestamp).status_code == 202
    assert upload(client, session_id).status_code == 429
    assert upload(client, session_id, frame_id=frame_id, timestamp=timestamp).status_code == 200


@pytest.mark.parametrize(
    "image,media_type,status",
    [(b"this is not a JPEG", "image/jpeg", 422), (b"not a PNG either", "image/png", 415)],
)
def test_invalid_images_are_rejected_before_enqueue(client, image, media_type, status):
    session_id = create_session(client)
    response = upload(client, session_id, image=image, media_type=media_type)
    assert response.status_code == status, response.text
    assert not any(event["type"] == "frame.analyzed" for event in events(client, session_id))


def test_image_byte_limit_is_enforced(client_factory):
    client = client_factory(max_image_bytes=1024, max_request_bytes=4096)
    session_id = create_session(client)
    response = upload(client, session_id, image=b"x" * 2048)
    assert response.status_code == 413, response.text


def test_chunked_multipart_request_limit_is_enforced_without_content_length(client_factory):
    client = client_factory(max_image_bytes=1024, max_request_bytes=4096)
    session_id = create_session(client)
    boundary = "looki-integration-boundary"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="frame_id"\r\n\r\n{uuid4()}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="timestamp"\r\n\r\n{datetime.now(UTC).isoformat()}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="large.jpg"\r\n'
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode()
        + b"x" * 5000
        + f"\r\n--{boundary}--\r\n".encode()
    )
    chunks = (body[index : index + 300] for index in range(0, len(body), 300))
    request = client.build_request(
        "POST",
        f"/api/v1/sessions/{session_id}/frames",
        content=chunks,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert "content-length" not in request.headers
    response = client.send(request)
    assert response.status_code == 413, response.text


def test_image_pixel_limit_is_enforced(client_factory):
    client = client_factory(max_image_pixels=100)
    session_id = create_session(client)
    response = upload(client, session_id)
    assert response.status_code == 413, response.text


def test_timestamp_must_include_timezone(client):
    session_id = create_session(client)
    response = upload(client, session_id, timestamp=datetime.now().isoformat())
    assert response.status_code == 422, response.text


def test_unknown_centres_and_invalid_context_identifiers_are_rejected(client):
    missing = client.post("/api/v1/sessions", json={"center_id": "missing-centre"})
    assert missing.status_code == 404, missing.text
    traversal = client.post("/api/v1/sessions", json={"center_id": "../demo-centre"})
    assert traversal.status_code == 422, traversal.text


@pytest.mark.parametrize("offset", [timedelta(minutes=-10), timedelta(minutes=10)])
def test_timestamp_outside_freshness_window_is_rejected(client, offset):
    session_id = create_session(client)
    response = upload(client, session_id, timestamp=(datetime.now(UTC) + offset).isoformat())
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("expires_during_inference", [False, True])
def test_stale_frames_never_produce_a_current_proximity_alert(
    client, monkeypatch, expires_during_inference
):
    session_id = create_session(client)
    frame_id = str(uuid4())
    now = datetime.now(UTC)
    calls = 0

    def analysis_clock():
        nonlocal calls
        calls += 1
        if expires_during_inference and calls == 1:
            return now
        return now + timedelta(minutes=10)

    monkeypatch.setattr("app.services.analysis.utcnow", analysis_clock)
    assert upload(client, session_id, frame_id=frame_id).status_code == 202
    failure = wait_for_event(client, session_id, "analysis.failed", frame_id=frame_id)
    assert failure["payload"]["code"] == "stale_frame"
    frame = client.get(f"/api/v1/sessions/{session_id}/frames/{frame_id}").json()
    assert frame["status"] == "failed"
    assert frame["error_code"] == "stale_frame"
    assert not any(event["type"] == "alert.proximity" for event in events(client, session_id))


def test_model_failure_is_reported_and_does_not_kill_the_worker(client):
    class BrokenProvider:
        def analyze(self, image):
            raise RuntimeError("Simulated model failure")

    session_id = create_session(client)
    original_provider = client.app.state.runtime.vision
    client.app.state.runtime.vision = BrokenProvider()
    failed_id = str(uuid4())
    assert upload(client, session_id, frame_id=failed_id).status_code == 202
    failure = wait_for_event(client, session_id, "analysis.failed", frame_id=failed_id)
    assert failure["payload"]["code"] == "inference_failed"
    failed_frame = client.get(f"/api/v1/sessions/{session_id}/frames/{failed_id}").json()
    assert failed_frame["status"] == "failed"
    assert failed_frame["detections"] == []
    assert not any(event["type"] == "alert.proximity" for event in events(client, session_id))

    client.app.state.runtime.vision = original_provider
    recovered_id = str(uuid4())
    assert upload(client, session_id, frame_id=recovered_id).status_code == 202
    wait_for_event(client, session_id, "alert.proximity", frame_id=recovered_id)


def test_sessions_and_frames_are_scoped_and_closed_sessions_reject_new_frames(client):
    first_session = create_session(client)
    second_session = create_session(client)
    frame_id = str(uuid4())
    assert upload(client, first_session, frame_id=frame_id).status_code == 202
    assert client.get(f"/api/v1/sessions/{second_session}/frames/{frame_id}").status_code == 404
    assert client.get(f"/api/v1/sessions/{uuid4()}").status_code == 404
    closed = client.post(f"/api/v1/sessions/{first_session}/close")
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"
    assert upload(client, first_session).status_code == 409


def test_event_cursor_is_exclusive_and_ordered(client):
    session_id = create_session(client)
    frame_id = str(uuid4())
    assert upload(client, session_id, frame_id=frame_id).status_code == 202
    alert = wait_for_event(client, session_id, "alert.proximity", frame_id=frame_id)
    all_events = events(client, session_id)
    ids = [event["id"] for event in all_events]
    assert ids == sorted(set(ids))
    assert all(event["session_id"] == session_id for event in all_events)
    assert all(event["timestamp"] for event in all_events)
    remaining = events(client, session_id, after=alert["id"])
    assert all(event["id"] > alert["id"] for event in remaining)


def test_pending_images_and_event_history_survive_application_restart(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'persistent.sqlite'}",
        api_key=API_KEY,
        create_schema=True,
        worker_enabled=False,
        vision_provider="mock",
        centres_provider="mock",
        worker_poll_seconds=0.01,
        websocket_poll_seconds=0.01,
        fixtures_dir=FIXTURE_DIR.parent,
    )
    with TestClient(create_app(settings), headers=HEADERS) as first:
        session_id = create_session(first)
        initial_events = events(first, session_id)
        frame_id = str(uuid4())
        assert upload(first, session_id, frame_id=frame_id).status_code == 202
        frame = first.get(f"/api/v1/sessions/{session_id}/frames/{frame_id}").json()
        assert frame["status"] == "pending"

    with TestClient(
        create_app(settings.model_copy(update={"worker_enabled": True})), headers=HEADERS
    ) as restarted:
        alert = wait_for_event(restarted, session_id, "alert.proximity", frame_id=frame_id)
        restored = events(restarted, session_id)
        assert restored[: len(initial_events)] == initial_events
        assert alert["id"] > initial_events[-1]["id"]
        assert sum(event["type"] == "alert.proximity" for event in restored) == 1
