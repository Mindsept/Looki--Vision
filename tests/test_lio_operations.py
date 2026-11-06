import logging
from datetime import timedelta
from uuid import UUID

import httpx
from sqlalchemy import select
from test_api import client_factory as client_factory  # noqa: F401
from test_api import create_session
from test_mobile_contract import call, emit, eventually, getq, question, scene
from test_mobile_contract import setup as setup  # noqa: F401

from app.core.config import Settings
from app.core.logging import TicketRedactionFilter
from app.core.time import utcnow
from app.models.entities import Session
from scripts.check_realtime import check
from scripts.purge_sessions import purge


def test_preflight_never_generates_or_leaks_provider_errors():
    settings = Settings(_env_file=None, openai_api_key="secret-value", realtime_enabled=True)
    paths = []

    def handle(request):
        paths.append((request.method, request.url.path))
        return httpx.Response(403, text="private upstream content secret-value")

    result, ok = check(settings, remote=True, transport=httpx.MockTransport(handle))
    assert not ok and result["provider_check"] == "http_403"
    assert paths == [("GET", "/v1/models/gpt-realtime-2.1")]
    assert "secret" not in str(result) and not result["real_audio_validated"]
    result, ok = check(Settings(_env_file=None), remote=True)
    assert not ok and result["provider_check"] == "missing_key"


def test_websocket_ticket_redacted_in_uvicorn_message_and_args():
    record = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        "",
        0,
        '%s - "WebSocket /ws?ticket=%s&after=3"',
        ("127.0.0.1", "secret-ticket"),
        None,
    )
    assert TicketRedactionFilter().filter(record)
    assert "secret-ticket" not in record.getMessage()
    assert "ticket=[redacted]&after=3" in record.getMessage()


def test_purge_dry_run_then_apply_preserves_active_sessions_and_cleans_audio(setup):
    client, provider, sid = setup
    c = call(client, sid)
    scene(client, sid)
    q, _ = question(client, sid, c)
    eventually(lambda: provider.generated, bool)
    emit(client, provider, "response.done")
    eventually(lambda: getq(client, sid, q), lambda v: v["status"] == "completed")
    client.post(f"/api/v1/sessions/{sid}/close")
    eventually(lambda: getq(client, sid, q), lambda v: v["media_status"] == "closed")
    active = create_session(client)

    async def exercise():
        db = client.app.state.runtime.db
        async with db.sessions() as connection, connection.begin():
            session = await connection.get(Session, UUID(sid))
            session.closed_at = utcnow() - timedelta(days=2)
        before = utcnow() - timedelta(days=1)
        preview = await purge(db, before)
        assert preview["counts"]["sessions"] == 1
        assert preview["counts"]["audio_questions"] == 1
        assert preview["counts"]["frames"] == 1
        assert not preview["applied"]
        async with db.sessions() as connection:
            assert await connection.get(Session, UUID(sid))
        result = await purge(db, before, apply=True)
        assert result["counts"] == preview["counts"]
        async with db.sessions() as connection:
            assert await connection.get(Session, UUID(active))
            assert await connection.get(Session, UUID(sid)) is None
        assert not result["backups_purged"]

    client.portal.call(exercise)


def test_purge_excludes_closed_session_with_pending_capture(client_factory):
    from test_api import upload

    client = client_factory(worker_enabled=False)
    sid = create_session(client)
    upload(client, sid)
    client.post(f"/api/v1/sessions/{sid}/close")

    async def exercise():
        db = client.app.state.runtime.db
        result = await purge(db, utcnow() + timedelta(days=1), apply=True)
        assert result["counts"]["sessions"] == 0
        async with db.sessions() as connection:
            assert await connection.scalar(select(Session).where(Session.id == UUID(sid)))

    client.portal.call(exercise)
