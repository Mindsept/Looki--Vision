from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import Settings
from app.main import create_app
from app.services.socket_tickets import SocketTickets

KEY = "live-test-key"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'live.sqlite'}",
        api_key=KEY,
        create_schema=True,
        worker_enabled=False,
        vision_provider="mock",
        fixtures_dir=FIXTURES,
        max_pending_frames=2,
    )
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {KEY}"}) as value:
        yield value


def session(client):
    response = client.post("/api/v1/sessions", json={})
    assert response.status_code == 201
    return response.json()["id"]


def send(client, sid, *, latest=True, frame_id=None, timestamp=None, image=None):
    return client.post(
        f"/api/v1/sessions/{sid}/frames",
        data={
            "frame_id": frame_id or str(uuid4()),
            "timestamp": timestamp or datetime.now(UTC).isoformat(),
            "latest_only": str(latest).lower(),
        },
        files={
            "image": (
                "frame.jpg",
                image or (FIXTURES / "images/near.jpg").read_bytes(),
                "image/jpeg",
            )
        },
    )


def test_latest_queue_keeps_one_pending_and_journals_superseded(client):
    sid = session(client)
    ids = [send(client, sid).json()["frame_id"] for _ in range(5)]
    states = [client.get(f"/api/v1/sessions/{sid}/frames/{fid}").json() for fid in ids]
    assert [s["status"] for s in states] == ["failed"] * 4 + ["pending"]
    assert all(s["error_code"] == "superseded_frame" for s in states[:-1])
    events = client.get(f"/api/v1/sessions/{sid}/events").json()
    skipped = [e for e in events if e["type"] == "analysis.failed"]
    assert len(skipped) == 4
    assert {e["payload"]["frame_id"] for e in skipped} == set(ids[:-1])
    assert not any(e["type"] == "alert.proximity" for e in events)


def test_fifo_default_is_preserved_and_other_sessions_are_never_superseded(client):
    a, b = session(client), session(client)
    first = send(client, a, latest=False).json()["frame_id"]
    send(client, b, latest=False)
    assert send(client, a, latest=False).status_code == 429
    assert send(client, b).status_code == 202
    assert client.get(f"/api/v1/sessions/{a}/frames/{first}").json()["status"] == "pending"


def test_invalid_latest_frame_cannot_discard_pending_work(client):
    sid = session(client)
    first = send(client, sid).json()["frame_id"]
    assert send(client, sid, image=b"not a JPEG").status_code == 422
    assert client.get(f"/api/v1/sessions/{sid}/frames/{first}").json()["status"] == "pending"


def test_old_capture_does_not_replace_newer_pending_and_retry_stays_idempotent(client):
    sid = session(client)
    stamp = datetime.now(UTC).isoformat()
    fid = str(uuid4())
    assert send(client, sid, frame_id=fid, timestamp=stamp).status_code == 202
    older = (datetime.now(UTC) - timedelta(seconds=3)).isoformat()
    assert send(client, sid, timestamp=older).json()["error"]["code"] == "out_of_order_frame"
    assert send(client, sid, frame_id=fid, timestamp=stamp).status_code == 200
    assert client.get(f"/api/v1/sessions/{sid}/frames/{fid}").json()["status"] == "pending"


def test_image_endpoint_returns_exact_bytes_and_is_session_scoped(client):
    sid, other = session(client), session(client)
    fid = send(client, sid).json()["frame_id"]
    response = client.get(f"/api/v1/sessions/{sid}/frames/{fid}/image")
    assert response.content == (FIXTURES / "images/near.jpg").read_bytes()
    assert response.headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/v1/sessions/{other}/frames/{fid}/image").status_code == 404
    assert (
        client.get(
            f"/api/v1/sessions/{sid}/frames/{fid}/image", headers={"Authorization": ""}
        ).status_code
        == 401
    )


def test_browser_ticket_authenticates_once_without_api_key_in_socket_url(client):
    sid = session(client)
    assert (
        client.post(f"/api/v1/sessions/{sid}/ws-ticket", headers={"Authorization": ""}).status_code
        == 401
    )
    ticket = client.post(f"/api/v1/sessions/{sid}/ws-ticket").json()["ticket"]
    url = f"/api/v1/sessions/{sid}/ws?ticket={ticket}"
    with client.websocket_connect(url, headers={"Authorization": ""}) as ws:
        assert ws.receive_json()["type"] == "session.state"
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(url, headers={"Authorization": ""}),
    ):
        pass


def test_tickets_expire_bind_to_session_and_are_bounded(monkeypatch):
    import app.services.socket_tickets as module

    clock = [10.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    tickets = SocketTickets(ttl=2, capacity=1)
    sid = uuid4()
    token = tickets.issue(sid)
    assert not tickets.consume(token, uuid4())
    token = tickets.issue(sid)
    clock[0] = 13
    assert not tickets.consume(token, sid)
    token = tickets.issue(sid)
    from app.core.errors import DomainError

    with pytest.raises(DomainError):
        tickets.issue(sid)
    assert tickets.consume(token, sid)


def test_runtime_never_exposes_credentials_and_missing_video_is_clear(client):
    response = client.get("/api/v1/runtime")
    assert response.status_code == 200
    assert KEY not in response.text and "password" not in response.text
    assert client.get("/api/v1/demo-video").status_code == 404
