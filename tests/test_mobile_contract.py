"""HTTP contracts exercised with real DB/vision worker and a controlled provider transport."""

import asyncio
import copy
import io
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect
from test_api import API_KEY, create_session, upload, wait_for_event
from test_api import client_factory as client_factory  # noqa: F401

from app.core.auth import issue_session_token
from app.models import AudioQuestion

SDP = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=recvonly\r\n"


class FakeSocket:
    def __init__(self):
        self.events = asyncio.Queue()
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.events.get()
        if event is None:
            raise StopAsyncIteration
        return json.dumps(event)

    async def close(self):
        self.closed = True
        await self.events.put(None)

    async def send(self, event):
        self.sent.append(json.loads(event))


class FakeProvider:
    def __init__(self):
        self.offers, self.generated, self.hung_up, self.interrupted = [], [], [], []
        self.fail_create = False
        self.fail_hangup = False

    async def create(self, offer):
        self.offers.append(offer)
        if self.fail_create:
            raise RuntimeError("upstream secret must not leak")
        return f"rtc_{uuid4().hex}", SDP

    async def attach(self, call_id):
        return FakeSocket()

    async def generate(self, socket, question, images):
        response_id = f"resp_{question.request_id}"
        self.generated.append(
            {
                "request_id": str(question.request_id),
                "context": copy.deepcopy(question.context),
                "images": images,
                "response_id": response_id,
                "socket": socket,
            }
        )
        await socket.events.put(
            {
                "type": "response.created",
                "response": {
                    "id": response_id,
                    "metadata": {"request_id": str(question.request_id)},
                },
            }
        )

    async def interrupt(self, socket, response_id):
        self.interrupted.append(response_id)

    async def hangup(self, call_id):
        self.hung_up.append(call_id)
        if self.fail_hangup:
            raise RuntimeError("hangup unavailable")

    async def close(self):
        pass


@pytest.fixture
def setup(monkeypatch, client_factory):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.realtime.OpenAIRealtimeProvider", lambda _: provider)
    client = client_factory(realtime_enabled=True, openai_api_key="test-only-not-a-real-key")
    return client, provider, create_session(client)


def eventually(fn, predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if predicate(value):
            return value
        time.sleep(0.01)
    pytest.fail(f"Timed out, last value: {value}")


def call(client, sid):
    body = {"request_id": str(uuid4()), "sdp_offer": SDP}
    result = client.post(f"/api/v1/sessions/{sid}/realtime/calls", json=body)
    assert result.status_code == 201, result.text
    value = result.json()
    return eventually(
        lambda: client.get(f"/api/v1/sessions/{sid}/realtime/calls/{value['call_id']}").json(),
        lambda v: v["control_status"] == "ready",
    )


def question(client, sid, c, **overrides):
    body = {
        "request_id": str(uuid4()),
        "call_id": c["call_id"],
        "media_epoch": c["media_epoch"],
        "text": "Décris devant moi.",
        "playback_ready": True,
        **overrides,
    }
    return body, client.post(f"/api/v1/sessions/{sid}/questions", json=body)


def scene(client, sid, count=1):
    ids = []
    for i in range(count):
        result = upload(
            client,
            sid,
            timestamp=(datetime.now(UTC) - timedelta(seconds=(count - i) * 0.05)).isoformat(),
        )
        assert result.status_code == 202
        fid = result.json()["frame_id"]
        wait_for_event(client, sid, "frame.analyzed", frame_id=fid)
        ids.append(fid)
    return ids


def getq(client, sid, q):
    return client.get(f"/api/v1/sessions/{sid}/questions/{q['request_id']}").json()


def emit(client, provider, kind, status="completed"):
    generated = provider.generated[-1]
    event = {
        "type": kind,
        "response_id": generated["response_id"],
        "response": {"id": generated["response_id"], "status": status},
        "transcript": "Une scène de test.",
    }
    client.portal.call(generated["socket"].events.put, event)


def encoded_image(format="JPEG", mode="RGB"):
    stream = io.BytesIO()
    Image.new(mode, (64, 48)).save(stream, format=format)
    return stream.getvalue()


def test_jpeg_ingestion_roundtrip_retry_and_invalid_formats(client_factory):
    client = client_factory()
    sid = create_session(client)
    fid, stamp = str(uuid4()), datetime.now(UTC).isoformat()
    original = encoded_image()
    response = upload(
        client, sid, image=original, media_type="image/jpeg", frame_id=fid, timestamp=stamp
    )
    assert response.status_code == 202
    assert response.json()["content_type"] == "image/jpeg"
    wait_for_event(client, sid, "frame.analyzed", frame_id=fid)
    returned = client.get(f"/api/v1/sessions/{sid}/frames/{fid}/image")
    assert returned.content == original and returned.headers["content-type"] == "image/jpeg"
    client.post(f"/api/v1/sessions/{sid}/close")
    assert (
        upload(
            client, sid, image=original, media_type="image/jpeg", frame_id=fid, timestamp=stamp
        ).status_code
        == 200
    )
    assert (
        upload(
            client, sid, image=original, media_type="image/png", frame_id=fid, timestamp=stamp
        ).status_code
        == 415
    )
    other = create_session(client)
    # Check declared MIME and actual encoding independently.
    assert (
        upload(client, other, image=encoded_image("PNG"), media_type="image/png").status_code == 415
    )
    assert (
        upload(client, other, image=encoded_image("PNG"), media_type="image/jpeg").status_code
        == 415
    )
    assert upload(client, other, image=original[:40], media_type="image/jpeg").status_code == 422
    assert upload(client, other, image=original, media_type="image/gif").status_code == 415


def test_historical_png_remains_readable_without_rewriting_stored_bytes(client_factory):
    from app.models import Frame

    client = client_factory()
    sid = create_session(client)
    fid = scene(client, sid)[0]
    historical = encoded_image("PNG")

    async def restore_historical_capture():
        runtime = client.app.state.runtime
        async with runtime.write_lock, runtime.db.sessions() as db, db.begin():
            row = await db.scalar(select(Frame).where(Frame.frame_id == UUID(fid)))
            row.image, row.content_type = historical, "image/png"

    client.portal.call(restore_historical_capture)
    path = f"/api/v1/sessions/{sid}/frames/{fid}"
    assert client.get(path).json()["content_type"] == "image/png"
    response = client.get(path + "/image")
    assert response.headers["content-type"] == "image/png"
    assert response.content == historical


def test_capabilities_isolate_sessions_roles_and_alert_socket(client_factory):
    client = client_factory()
    sid, other = create_session(client), create_session(client)
    tokens = client.post(f"/api/v1/sessions/{sid}/access-tokens").json()
    mobile = {"Authorization": f"Bearer {tokens['mobile_token']}"}
    pi = {"Authorization": f"Bearer {tokens['pi_token']}"}
    assert client.get(f"/api/v1/sessions/{sid}", headers=mobile).status_code == 200
    assert client.get(f"/api/v1/sessions/{other}", headers=mobile).status_code == 403
    assert client.post(f"/api/v1/sessions/{sid}/access-tokens", headers=mobile).status_code == 403
    assert client.post(f"/api/v1/sessions/{sid}/questions", headers=pi, json={}).status_code == 403
    assert client.post(f"/api/v1/sessions/{sid}/frames", headers=mobile).status_code == 403
    assert client.post("/api/v1/sessions", headers=pi, json={}).status_code == 403
    expired = issue_session_token(API_KEY, UUID(sid), "mobile", -1)
    assert (
        client.get(
            f"/api/v1/sessions/{sid}", headers={"Authorization": f"Bearer {expired}"}
        ).status_code
        == 401
    )
    with client.websocket_connect(f"/api/v1/sessions/{sid}/ws", headers=mobile) as ws:
        assert ws.receive_json()["type"] == "session.state"
        ws.send_json({"type": "question", "request_id": str(uuid4()), "text": "Bonjour"})
        for _ in range(4):
            event = ws.receive_json()
            if event["type"] == "error":
                assert event["payload"]["code"] == "http_question_required"
                break
        else:
            pytest.fail("Legacy question accepted with mobile capability")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/v1/sessions/{sid}/ws", headers=pi):
            pass


def test_calls_are_idempotent_and_do_not_generate(setup):
    client, provider, sid = setup
    body = {"request_id": str(uuid4()), "sdp_offer": SDP}
    first = client.post(f"/api/v1/sessions/{sid}/realtime/calls", json=body)
    duplicate = client.post(f"/api/v1/sessions/{sid}/realtime/calls", json=body)
    assert (first.status_code, duplicate.status_code) == (201, 200)
    assert first.json()["call_id"] == duplicate.json()["call_id"]
    eventually(lambda: provider.offers, lambda v: len(v) == 1)
    assert not provider.generated
    conflict = client.post(
        f"/api/v1/sessions/{sid}/realtime/calls",
        json={**body, "sdp_offer": SDP.replace("o=- 1", "o=- 2")},
    )
    assert conflict.status_code == 409
    active = client.post(
        f"/api/v1/sessions/{sid}/realtime/calls", json={**body, "request_id": str(uuid4())}
    )
    assert active.json()["error"]["code"] == "call_active"
    current = client.get(f"/api/v1/sessions/{sid}/realtime/calls/current").json()
    assert current["call_id"] == first.json()["call_id"]


@pytest.mark.parametrize(
    "offer", ["not SDP", SDP.replace("recvonly", "sendrecv"), SDP.replace("recvonly", "sendonly")]
)
def test_invalid_or_microphone_sdp_does_not_reach_provider(setup, offer):
    client, provider, sid = setup
    result = client.post(
        f"/api/v1/sessions/{sid}/realtime/calls",
        json={"request_id": str(uuid4()), "sdp_offer": offer},
    )
    assert result.status_code == 422 and result.json()["error"]["code"] == "invalid_sdp"
    assert not provider.offers


def test_no_scene_no_generation_and_disabled_provider_does_not_break_alerts(setup, client_factory):
    client, provider, sid = setup
    c = call(client, sid)
    q, response = question(client, sid, c)
    assert response.status_code == 202
    assert response.json()["status"] == "unavailable"
    assert response.json()["sound_id"] == "v1.scene_unavailable"
    assert not provider.generated
    disabled = client_factory()
    sid2 = create_session(disabled)
    denied = disabled.post(
        f"/api/v1/sessions/{sid2}/realtime/calls",
        json={"request_id": str(uuid4()), "sdp_offer": SDP},
    )
    assert denied.status_code == 503
    frame = upload(disabled, sid2).json()
    alert = wait_for_event(disabled, sid2, "alert.proximity", frame_id=frame["frame_id"])
    assert alert["payload"]["sound_id"] == "v1.proximity_generic"
    assert datetime.fromisoformat(alert["payload"]["expires_at"]) - datetime.fromisoformat(
        alert["payload"]["captured_at"]
    ) == timedelta(seconds=3)


def test_question_freezes_latest_three_and_retry_never_generates_twice(setup):
    client, provider, sid = setup
    c = call(client, sid)
    frames = scene(client, sid, 4)
    q, result = question(client, sid, c)
    assert result.status_code == 202
    eventually(lambda: provider.generated, lambda v: len(v) == 1)
    generated = provider.generated[0]
    snapshot = copy.deepcopy(generated["context"])
    assert [f["frame_id"] for f in snapshot["frames"]] == frames[-3:]
    assert len(generated["images"]) == 3
    assert all(i["image_url"].startswith("data:image/jpeg;base64,") for i in generated["images"])
    assert not snapshot["partial_context"]
    scene(client, sid)
    assert generated["context"] == snapshot
    retry = client.post(f"/api/v1/sessions/{sid}/questions", json=q)
    assert retry.status_code == 200
    assert len(provider.generated) == 1
    conflict = client.post(
        f"/api/v1/sessions/{sid}/questions", json={**q, "text": "Autre question"}
    )
    assert conflict.status_code == 409
    assert question(client, sid, c)[1].json()["error"]["code"] == "answer_active"


def test_completed_generation_remains_busy_until_playback_finishes(setup):
    client, provider, sid = setup
    c = call(client, sid)
    scene(client, sid)
    q, _ = question(client, sid, c)
    eventually(lambda: provider.generated, lambda v: len(v) == 1)
    assert provider.generated[0]["context"]["partial_context"]
    path = f"/api/v1/sessions/{sid}/questions/{q['request_id']}"
    state = {
        "call_id": c["call_id"],
        "media_epoch": c["media_epoch"],
        "sequence": 1,
        "state": "finished",
        "position_ms": None,
        "measurement": "unavailable",
    }
    assert client.put(path + "/playback", json=state).status_code == 409
    emit(client, provider, "response.output_audio_transcript.done")
    emit(client, provider, "response.done")
    eventually(lambda: getq(client, sid, q), lambda v: v["status"] == "completed")
    assert question(client, sid, c)[1].status_code == 409
    assert client.put(path + "/playback", json=state).status_code == 200
    assert getq(client, sid, q)["media_status"] == "closed"
    assert client.put(path + "/playback", json=state).status_code == 200
    assert client.put(path + "/playback", json={**state, "state": "playing"}).status_code == 409
    ignored = client.put(path + "/playback", json={**state, "sequence": 2, "state": "playing"})
    assert ignored.json()["state"] == "finished"
    assert getq(client, sid, q)["transcript"] == "Une scène de test."
    assert question(client, sid, c)[1].status_code == 202


def test_cancel_completed_audio_closes_call_preserves_status_and_changes_epoch(setup):
    client, provider, sid = setup
    c = call(client, sid)
    scene(client, sid)
    q, _ = question(client, sid, c)
    eventually(lambda: provider.generated, lambda v: bool(v))
    emit(client, provider, "response.done")
    eventually(lambda: getq(client, sid, q), lambda v: v["status"] == "completed")
    body = {"call_id": c["call_id"], "media_epoch": c["media_epoch"], "reason": "alert"}
    path = f"/api/v1/sessions/{sid}/questions/{q['request_id']}/cancel"
    assert client.post(path, json=body).status_code == 202
    final = eventually(lambda: getq(client, sid, q), lambda v: v["media_status"] == "closed")
    assert final["status"] == "completed"
    assert client.post(path, json=body).status_code == 200
    assert provider.interrupted and provider.hung_up
    c2 = call(client, sid)
    assert c2["media_epoch"] == c["media_epoch"] + 1
    assert question(client, sid, c)[1].json()["error"]["code"] == "stale_media_epoch"


def test_playback_sequence_and_interruption_are_monotonic(setup):
    client, provider, sid = setup
    c = call(client, sid)
    scene(client, sid)
    q, _ = question(client, sid, c)
    path = f"/api/v1/sessions/{sid}/questions/{q['request_id']}/playback"
    state = {
        "call_id": c["call_id"],
        "media_epoch": c["media_epoch"],
        "sequence": 2,
        "state": "playing",
        "position_ms": 400,
        "measurement": "estimated",
    }
    assert client.put(path, json=state).status_code == 200
    assert client.put(path, json={**state, "sequence": 1}).json()["sequence"] == 2
    assert client.put(path, json={**state, "position_ms": 500}).status_code == 409
    interrupted = client.put(path, json={**state, "sequence": 3, "state": "interrupted"})
    assert interrupted.status_code == 200
    eventually(lambda: getq(client, sid, q), lambda v: v["media_status"] == "closed")
    assert getq(client, sid, q)["status"] == "cancelled"


def test_restart_recovery_never_replays_generation(setup):
    client, provider, sid = setup
    c = call(client, sid)
    scene(client, sid)
    q, _ = question(client, sid, c)
    eventually(lambda: provider.generated, lambda v: bool(v))
    client.portal.call(client.app.state.runtime.realtime.recover)
    row = eventually(lambda: getq(client, sid, q), lambda v: v["media_status"] == "closed")
    assert row["status"] == "failed" and row["error"]["code"] == "backend_restarted"
    assert client.post(f"/api/v1/sessions/{sid}/questions", json=q).status_code == 200
    assert len(provider.generated) == 1


def test_lost_control_and_failed_hangup_block_media_reuse(setup):
    client, provider, sid = setup
    c = call(client, sid)
    scene(client, sid)
    q, _ = question(client, sid, c)
    eventually(lambda: provider.generated, lambda v: bool(v))
    provider.fail_hangup = True
    client.portal.call(provider.generated[0]["socket"].close)
    row = eventually(lambda: getq(client, sid, q), lambda v: v["status"] == "failed")
    assert row["media_status"] == "stopping"
    assert (
        client.post(
            f"/api/v1/sessions/{sid}/realtime/calls",
            json={"request_id": str(uuid4()), "sdp_offer": SDP},
        ).status_code
        == 409
    )
    provider.fail_hangup = False
    client.delete(f"/api/v1/sessions/{sid}/realtime/calls/{c['call_id']}")
    eventually(lambda: getq(client, sid, q), lambda v: v["media_status"] == "closed")


def test_expired_scene_before_dispatch_does_not_call_model(setup, monkeypatch):
    client, provider, sid = setup
    c = call(client, sid)
    scene(client, sid)
    runtime = client.app.state.runtime
    original = runtime.realtime.spawn

    def hold_generation(key, coro):
        if key[0] == "generate":
            coro.close()
        else:
            original(key, coro)

    monkeypatch.setattr(runtime.realtime, "spawn", hold_generation)
    q, _ = question(client, sid, c)

    async def expire_and_dispatch():
        async with runtime.write_lock, runtime.db.sessions() as db, db.begin():
            row = await db.scalar(
                select(AudioQuestion).where(AudioQuestion.request_id == UUID(q["request_id"]))
            )
            ctx = copy.deepcopy(row.context)
            ctx["frames"][0]["captured_at"] = (
                datetime.now(UTC) - timedelta(seconds=30)
            ).isoformat()
            row.context = ctx
            question_id = row.id
        await runtime.realtime._generate(question_id)

    client.portal.call(expire_and_dispatch)
    assert getq(client, sid, q)["status"] == "unavailable"
    assert not provider.generated


def test_closing_session_closes_realtime_and_retains_idempotent_question(setup):
    client, provider, sid = setup
    c = call(client, sid)
    scene(client, sid)
    q, _ = question(client, sid, c)
    assert client.post(f"/api/v1/sessions/{sid}/close").status_code == 200
    eventually(lambda: getq(client, sid, q), lambda v: v["media_status"] == "closed")
    assert client.post(f"/api/v1/sessions/{sid}/questions", json=q).status_code == 200
    assert question(client, sid, c)[1].json()["error"]["code"] == "session_closed"


@pytest.mark.parametrize("media", ["m=audio ", "m=video 9"])
def test_malformed_sdp_is_validation_error_not_server_error(setup, media):
    client, _, sid = setup
    bad = f"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n{media}\r\na=recvonly\r\n"
    response = client.post(
        f"/api/v1/sessions/{sid}/realtime/calls",
        json={
            "request_id": str(uuid4()),
            "sdp_offer": bad,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_sdp"


@pytest.mark.parametrize("completed", [False, True])
def test_generation_or_playback_deadline_closes_media_without_replay(setup, completed):
    client, provider, sid = setup
    c = call(client, sid)
    scene(client, sid)
    q, _ = question(client, sid, c)
    eventually(lambda: provider.generated, lambda v: bool(v))
    if completed:
        emit(client, provider, "response.done")
        eventually(lambda: getq(client, sid, q), lambda v: v["status"] == "completed")

    async def expire():
        runtime = client.app.state.runtime
        async with runtime.write_lock, runtime.db.sessions() as db, db.begin():
            row = await db.scalar(
                select(AudioQuestion).where(AudioQuestion.request_id == UUID(q["request_id"]))
            )
            row.deadline_at = datetime.now(UTC) - timedelta(seconds=1)

    client.portal.call(expire)
    final = eventually(lambda: getq(client, sid, q), lambda v: v["media_status"] == "closed")
    assert final["status"] == ("completed" if completed else "failed")
    assert final["error"]["code"] == "timeout"
    assert provider.hung_up
    assert client.post(f"/api/v1/sessions/{sid}/questions", json=q).status_code == 200
    assert len(provider.generated) == 1


def test_close_during_provider_creation_never_exposes_ready(setup, monkeypatch):
    client, provider, sid = setup
    gate = asyncio.Event()
    original = provider.create

    async def slow_create(offer):
        await gate.wait()
        return await original(offer)

    monkeypatch.setattr(provider, "create", slow_create)
    base = f"/api/v1/sessions/{sid}/realtime/calls"
    result = client.post(base, json={"request_id": str(uuid4()), "sdp_offer": SDP})
    cid = result.json()["call_id"]
    assert client.delete(base + "/" + cid).status_code == 202
    client.portal.call(gate.set)
    final = eventually(
        lambda: client.get(base + "/" + cid).json(), lambda v: v["control_status"] == "closed"
    )
    assert final["sdp_answer"] is None
    assert not provider.generated
    # Creation may be skipped if cancellation wins before its task starts.
    assert len(provider.hung_up) == len(provider.offers)
