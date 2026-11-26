"""A viewer joins an existing Pi session without taking ownership of capture."""

import time
from uuid import uuid4

from test_api import client_factory as client_factory  # noqa: F401
from test_api import create_session, upload, wait_for_event

from app.repositories.events import EventRepository


def test_pi_viewer_scope_and_session_remains_active(client_factory):
    client = client_factory()
    sid = create_session(client)
    other = create_session(client)
    tokens = client.post(f"/api/v1/sessions/{sid}/access-tokens").json()
    route = f"/api/v1/sessions/{sid}/live"
    mobile = {"Authorization": "Bearer " + tokens["mobile_token"]}
    pi = {"Authorization": "Bearer " + tokens["pi_token"]}
    snapshot = client.get(route, headers=mobile)
    assert snapshot.status_code == 200
    assert snapshot.headers["cache-control"] == "no-store"
    value = snapshot.json()
    assert value["session"]["id"] == sid
    assert value["session"]["status"] == "active"
    assert value["latest_frame"] is None
    assert value["latest_depth"] is None
    assert value["runtime"]["vision_provider"] == "mock"
    assert client.get(route, headers=pi).status_code == 403
    assert client.get(route, headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert client.get(f"/api/v1/sessions/{other}/live", headers=mobile).status_code == 403
    assert client.get(f"/api/v1/sessions/{sid}", headers=pi).json()["status"] == "active"


def test_bounded_snapshot_keeps_last_image_and_wss_resumes_after_cursor(client_factory):
    client = client_factory()
    sid = create_session(client)
    frame_id = str(uuid4())
    assert upload(client, sid, frame_id=frame_id).status_code == 202
    wait_for_event(client, sid, "frame.analyzed", frame_id=frame_id)
    runtime = client.app.state.runtime

    async def add_history():
        from uuid import UUID

        async with runtime.write_lock, runtime.db.sessions() as db, db.begin():
            for _ in range(120):
                await EventRepository(db).add(UUID(sid), "session.state", {"status": "active"})

    client.portal.call(add_history)
    tokens = client.post(f"/api/v1/sessions/{sid}/access-tokens").json()
    mobile = {"Authorization": "Bearer " + tokens["mobile_token"]}
    value = client.get(f"/api/v1/sessions/{sid}/live", headers=mobile).json()
    assert len(value["recent_events"]) == 100
    assert value["latest_frame"]["payload"]["frame_id"] == frame_id
    assert value["latest_frame"]["id"] < value["recent_events"][0]["id"]
    assert value["cursor"] == value["recent_events"][-1]["id"]

    ticket = client.post(f"/api/v1/sessions/{sid}/ws-ticket", headers=mobile).json()["ticket"]
    new_frame = str(uuid4())
    assert upload(client, sid, frame_id=new_frame).status_code == 202
    expected = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        fresh = client.get(f"/api/v1/sessions/{sid}/events?after={value['cursor']}").json()
        expected = next(
            (
                e
                for e in fresh
                if e["type"] == "frame.analyzed" and e["payload"]["frame_id"] == new_frame
            ),
            None,
        )
        if expected:
            break
        time.sleep(0.02)
    assert expected is not None
    with client.websocket_connect(
        f"/api/v1/sessions/{sid}/ws?ticket={ticket}&after={value['cursor']}"
    ) as ws:
        assert ws.receive_json()["type"] == "session.state"
        for _ in range(8):
            event = ws.receive_json()
            assert event["id"] > value["cursor"]
            if event["type"] == "frame.analyzed":
                assert event == expected
                break
        else:
            raise AssertionError("Missing result after snapshot cursor")
    assert client.get(f"/api/v1/sessions/{sid}").json()["status"] == "active"
