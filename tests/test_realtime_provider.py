from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_mobile_contract import SDP, FakeSocket

from app.core.config import Settings
from app.core.errors import DomainError
from app.providers.realtime import OpenAIRealtimeProvider


@pytest.mark.asyncio
async def test_real_adapter_signaling_and_hangup_use_expected_provider_contract():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/v1/realtime/calls":
            return httpx.Response(
                201, headers={"Location": "/v1/realtime/calls/rtc_test"}, text=SDP
            )
        return httpx.Response(200)

    provider = OpenAIRealtimeProvider(Settings(_env_file=None, openai_api_key="test-key"))
    await provider.http.aclose()
    provider.http = httpx.AsyncClient(
        base_url="https://api.openai.com/v1",
        transport=httpx.MockTransport(handle),
        headers={"Authorization": "Bearer test-key"},
    )
    call_id, answer = await provider.create(SDP)
    assert call_id == "rtc_test" and answer == SDP
    request = requests[0]
    assert request.headers["content-type"].startswith("multipart/form-data;")
    assert b'name="sdp"' in request.content and b'name="session"' in request.content
    assert b'"turn_detection": null' in request.content
    assert b'"output_modalities": ["audio"]' in request.content
    await provider.hangup(call_id)
    assert requests[-1].url.path == "/v1/realtime/calls/rtc_test/hangup"
    await provider.close()


@pytest.mark.asyncio
async def test_real_adapter_attaches_same_call_and_waits_for_configuration_ack(monkeypatch):
    socket = FakeSocket()
    await socket.events.put({"type": "session.created"})
    await socket.events.put({"type": "session.updated"})
    connected = {}

    async def connect(url, **kwargs):
        connected.update(url=url, **kwargs)
        return socket

    monkeypatch.setattr("app.providers.realtime.connect", connect)
    provider = OpenAIRealtimeProvider(Settings(_env_file=None, openai_api_key="test-key"))
    assert await provider.attach("rtc_test") is socket
    assert connected["url"] == "wss://api.openai.com/v1/realtime?call_id=rtc_test"
    assert socket.sent[0]["session"]["audio"]["input"]["turn_detection"] is None
    assert not any(e["type"] == "response.create" for e in socket.sent)
    q = SimpleNamespace(
        request_id=uuid4(),
        context_id=uuid4(),
        context={"frames": [{"image_ref": "image-1"}], "question": "Que vois-tu ?"},
    )
    image = {"type": "input_image", "image_url": "data:image/png;base64,test"}
    await provider.generate(socket, q, [image])
    event = socket.sent[-1]
    assert event["type"] == "response.create"
    assert event["response"]["conversation"] == "none"
    assert event["response"]["metadata"]["request_id"] == str(q.request_id)
    assert event["response"]["input"][0]["content"][-1] == image
    await provider.interrupt(socket, "resp_test")
    assert [e["type"] for e in socket.sent[-2:]] == ["response.cancel", "output_audio_buffer.clear"]
    await provider.close()


@pytest.mark.asyncio
async def test_provider_errors_are_sanitized_and_not_retried():
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(401, json={"error": "secret provider detail"})

    provider = OpenAIRealtimeProvider(Settings(_env_file=None))
    await provider.http.aclose()
    provider.http = httpx.AsyncClient(
        base_url="https://api.openai.com/v1", transport=httpx.MockTransport(handle)
    )
    with pytest.raises(DomainError) as err:
        await provider.create(SDP)
    assert "secret" not in str(err.value) and count == 1
    await provider.close()


def test_pi_retry_reuses_bytes_ids_and_capture_timestamp(monkeypatch):
    from scripts.pi_upload import send_frame

    monkeypatch.setattr("scripts.pi_upload.time.sleep", lambda _: None)
    requests = []

    def handle(request):
        requests.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "1"})
            if len(requests) == 1
            else httpx.Response(202, json={"status": "pending"})
        )

    with httpx.Client(
        base_url="https://looki.example", transport=httpx.MockTransport(handle)
    ) as client:
        result = send_frame(client, uuid4(), b"png-contents", uuid4(), "2026-10-30T12:00:00Z")
    assert result["status"] == "pending"
    # Multipart boundaries can differ; every semantic field and image stays identical.
    for request in requests:
        assert b"png-contents" in request.content and b"2026-10-30T12:00:00Z" in request.content
        assert b"image/png" in request.content
    assert (
        requests[0].content.split(b'name="frame_id"')[1].split(b"\r\n--")[0]
        == requests[1].content.split(b'name="frame_id"')[1].split(b"\r\n--")[0]
    )


def test_pi_timeout_reads_same_capture_before_retry():
    from scripts.pi_upload import send_frame

    requests = []
    fid = uuid4()

    def handle(request):
        requests.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("lost acknowledgement")
        assert request.url.path.endswith(str(fid))
        return httpx.Response(200, json={"status": "completed"})

    with httpx.Client(
        base_url="https://looki.example", transport=httpx.MockTransport(handle)
    ) as client:
        assert (
            send_frame(client, uuid4(), b"png", fid, "2026-10-30T12:00:00Z")["status"]
            == "completed"
        )
    assert [r.method for r in requests] == ["POST", "GET"]
