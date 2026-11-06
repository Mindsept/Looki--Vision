from datetime import timedelta
from uuid import UUID

import pytest
from test_api import API_KEY, create_session
from test_api import client_factory as client_factory  # noqa: F401
from test_mobile_contract import (
    call,
    emit,
    eventually,
    getq,
    question,
    scene,
)
from test_mobile_contract import setup as setup  # noqa: F401

from app.core.auth import issue_session_token
from app.core.config import Settings
from app.models import AudioQuestion
from app.services.generation_metrics import token_usage


def test_configuration_is_session_scoped_and_never_calls_provider(setup):
    client, provider, sid = setup
    token = issue_session_token(API_KEY, sid, "mobile", 60)
    headers = {"Authorization": f"Bearer {token}"}
    result = client.get(f"/api/v1/sessions/{sid}/assistant", headers=headers)
    assert result.status_code == 200
    data = result.json()
    assert data["model"] == "gpt-realtime-2.1"
    assert data["input_mode"] == "local_stt_text"
    assert data["configuration_status"] == "configured"
    assert data["prompt_version"] == "lio.scene.v1.1"
    assert not provider.offers and not provider.generated
    assert "test-only-not-a-real-key" not in result.text
    other = create_session(client)
    assert client.get(f"/api/v1/sessions/{other}/assistant", headers=headers).status_code == 403
    pi = issue_session_token(API_KEY, sid, "pi", 60)
    assert (
        client.get(
            f"/api/v1/sessions/{sid}/assistant", headers={"Authorization": f"Bearer {pi}"}
        ).status_code
        == 403
    )


@pytest.mark.parametrize("enabled,expected", [(False, "disabled"), (True, "missing_key")])
def test_configuration_without_key_does_not_disable_vision(client_factory, enabled, expected):
    client = client_factory(realtime_enabled=enabled, openai_api_key="")
    sid = create_session(client)
    assert (
        client.get(f"/api/v1/sessions/{sid}/assistant").json()["configuration_status"] == expected
    )
    assert scene(client, sid)


def test_generation_measurements_are_persisted_and_separate_from_playback(setup):
    client, provider, sid = setup
    c = call(client, sid)
    frames = scene(client, sid, 2)
    q, admitted = question(client, sid, c)
    assert admitted.json()["scene"]["frame_ids"] == frames
    assert admitted.json()["scene"]["partial"] is True
    assert admitted.json()["scene"]["simulated"] is True
    assert admitted.json()["generation"]["usage"] is None
    eventually(lambda: provider.generated, bool)
    emit(client, provider, "output_audio_buffer.started")
    generated = provider.generated[-1]
    event = {
        "type": "response.done",
        "response": {
            "id": generated["response_id"],
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_audio", "transcript": "Scène partielle."}],
                }
            ],
            "usage": {
                "total_tokens": 30,
                "input_tokens": 20,
                "output_tokens": 10,
                "input_token_details": {
                    "image_tokens": 8,
                    "cached_tokens": 4,
                    "cached_tokens_details": {"image_tokens": 4},
                },
                "output_token_details": {"audio_tokens": 6},
                "secret": "not persisted",
            },
        },
    }
    client.portal.call(generated["socket"].events.put, event)
    result = eventually(lambda: getq(client, sid, q), lambda v: v["status"] == "completed")
    assert result["transcript"] == "Scène partielle."
    metrics = result["generation"]
    assert metrics["model"] == "gpt-realtime-2.1"
    assert metrics["requested_at"] <= metrics["submitted_at"] <= metrics["provider_created_at"]
    assert metrics["provider_audio_started_at"] <= metrics["finished_at"]
    assert metrics["usage"]["input_image_tokens"] == 8
    assert metrics["usage"]["cached_image_tokens"] == 4
    assert "secret" not in str(metrics)
    assert result["media_status"] == "reserved"
    assert result["playback"]["state"] == "ready"
    # Repeated completion cannot rewrite the durable measurements.
    event["response"]["usage"]["total_tokens"] = 9999
    client.portal.call(generated["socket"].events.put, event)
    assert getq(client, sid, q)["generation"]["usage"]["total_tokens"] == 30


def test_incomplete_audio_is_failure_with_usage_and_closes_call(setup):
    client, provider, sid = setup
    c = call(client, sid)
    scene(client, sid)
    q, _ = question(client, sid, c)
    eventually(lambda: provider.generated, bool)
    emit(client, provider, "response.done", status="incomplete")
    result = eventually(lambda: getq(client, sid, q), lambda v: v["media_status"] == "closed")
    assert result["status"] == "failed"
    assert result["error"]["code"] == "generation_incomplete"
    assert result["generation"]["provider_status"] == "incomplete"
    assert result["generation"]["usage"] is None  # Unknown is never billed as zero.
    assert len(provider.generated) == 1


def test_rate_limit_is_durable_and_exact_retries_bypass_it(setup):
    client, provider, sid = setup
    c = call(client, sid)
    q, result = question(client, sid, c)  # No frame: unavailable, media immediately closed.
    assert result.status_code == 202
    path = f"/api/v1/sessions/{sid}/questions"
    assert client.post(path, json=q).status_code == 200
    blocked = question(client, sid, c)[1]
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "question_rate_limited"

    async def advance_admission():
        async with client.app.state.runtime.db.sessions() as db, db.begin():
            from sqlalchemy import select

            row = await db.scalar(
                select(AudioQuestion).where(AudioQuestion.request_id == UUID(q["request_id"]))
            )
            row.created_at -= timedelta(seconds=3)

    client.portal.call(advance_admission)
    assert question(client, sid, c)[1].status_code == 202
    assert not provider.generated


def test_usage_ignores_secrets_and_invalid_values():
    assert token_usage(None) is None
    assert token_usage({"input_tokens": True, "output_tokens": -1, "total_tokens": "9"}) is None
    assert token_usage({"input_token_details": "bad", "output_tokens": 0}) == {"output_tokens": 0}


@pytest.mark.parametrize(
    "field,value",
    [
        ("realtime_max_output_tokens", 0),
        ("realtime_max_output_tokens", 5000),
        ("realtime_reasoning_effort", "none"),
        ("realtime_voice", "unknown"),
    ],
)
def test_invalid_provider_configuration_fails_early(field, value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
