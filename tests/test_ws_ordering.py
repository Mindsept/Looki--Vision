"""Un retry de question ne doit jamais faire dépasser une alerte non livrée."""

import asyncio
import threading
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.repositories.events import EventRepository
from app.schemas.api import Question


def test_duplicate_answer_cannot_overtake_unpublished_alert(tmp_path, monkeypatch):
    app = create_app(
        Settings(
            _env_file=None,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'ordering.sqlite'}",
            create_schema=True,
            worker_enabled=False,
            websocket_poll_seconds=0.01,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer looki-local-dev-key"}) as client:
        session_id = UUID(client.post("/api/v1/sessions", json={}).json()["id"])
        initial = client.get(f"/api/v1/sessions/{session_id}/events").json()[-1]["id"]
        runtime = app.state.runtime
        original_events = runtime.sessions.events
        publish_entered = threading.Event()

        async def make_gate():
            return asyncio.Event()

        gate = client.portal.call(make_gate)

        async def gated_events(*args, **kwargs):
            # Hold the publisher while receive() handles a retransmitted question.
            # An explicit barrier makes the regression independent of scheduling.
            publish_entered.set()
            await gate.wait()
            return await original_events(*args, **kwargs)

        monkeypatch.setattr(runtime.sessions, "events", gated_events)
        with client.websocket_connect(f"/api/v1/sessions/{session_id}/ws?after={initial}") as ws:
            assert ws.receive_json()["id"] is None
            assert publish_entered.wait(timeout=2)

            async def create_prior_alert():
                async with runtime.write_lock, runtime.db.sessions() as db, db.begin():
                    event = await EventRepository(db).add(
                        session_id, "alert.proximity", {"text": "Alerte avant la réponse."}
                    )
                    return event.id

            alert_id = client.portal.call(create_prior_alert)
            question = Question(type="question", request_id=uuid4(), text="Que vois-tu ?")
            answer, duplicate = client.portal.call(runtime.questions.answer, session_id, question)
            assert duplicate is False
            assert answer.id > alert_id

            ws.send_json(question.model_dump(mode="json"))
            ws.send_json({"type": "ping"})
            # Without the fix, the duplicate answer is the next message, moving
            # the mobile cursor past alert_id before the publisher can send it.
            assert ws.receive_json()["type"] == "pong"

            async def release_publisher():
                gate.set()

            client.portal.call(release_publisher)
            alert = ws.receive_json()
            delivered_answer = ws.receive_json()
            assert (alert["id"], alert["type"]) == (alert_id, "alert.proximity")
            assert (delivered_answer["id"], delivered_answer["type"]) == (answer.id, "answer")

        # A reconnect at the last safe cursor preserves exclusive replay semantics.
        monkeypatch.setattr(runtime.sessions, "events", original_events)
        remaining = client.get(f"/api/v1/sessions/{session_id}/events?after={answer.id}")
        assert remaining.json() == []
