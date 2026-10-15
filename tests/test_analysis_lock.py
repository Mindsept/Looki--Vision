"""Une analyse expirée pendant l'attente du verrou ne produit aucune alerte."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.time import as_utc
from app.main import create_app
from app.repositories.frames import FrameRepository
from app.services import analysis


def test_frame_expiring_while_waiting_for_write_lock_is_failed(tmp_path, monkeypatch):
    app = create_app(
        Settings(
            _env_file=None,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'expiry.sqlite'}",
            create_schema=True,
            worker_enabled=False,
            max_frame_age_seconds=30,
        )
    )
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "images" / "near.jpg"
    with TestClient(app, headers={"Authorization": "Bearer looki-local-dev-key"}) as client:
        session_id = client.post("/api/v1/sessions", json={}).json()["id"]
        frame_id = str(uuid4())
        response = client.post(
            f"/api/v1/sessions/{session_id}/frames",
            data={"frame_id": frame_id, "timestamp": datetime.now(UTC).isoformat()},
            files={"image": ("near.jpg", fixture.read_bytes(), "image/jpeg")},
        )
        assert response.status_code == 202
        runtime = app.state.runtime

        async def expire_during_lock_wait():
            async with runtime.write_lock, runtime.db.sessions() as db, db.begin():
                frame = await FrameRepository(db).claim_next()
            clock = {"now": as_utc(frame.captured_at)}
            monkeypatch.setattr(analysis, "utcnow", lambda: clock["now"])
            real_lock = runtime.write_lock
            attempted_acquisition = asyncio.Event()

            class ObservableLock:
                async def __aenter__(self):
                    attempted_acquisition.set()
                    await real_lock.acquire()
                    return self

                async def __aexit__(self, *_):
                    real_lock.release()

            monkeypatch.setattr(runtime, "write_lock", ObservableLock())
            await real_lock.acquire()
            task = asyncio.create_task(runtime.worker.process(frame))
            try:
                # This barrier is reached only after inference and its first
                # freshness check. Advancing a fake clock avoids timed sleeps.
                await asyncio.wait_for(attempted_acquisition.wait(), timeout=2)
                clock["now"] += timedelta(seconds=31)
            finally:
                real_lock.release()
            await asyncio.wait_for(task, timeout=2)

        client.portal.call(expire_during_lock_wait)
        frame = client.get(f"/api/v1/sessions/{session_id}/frames/{frame_id}").json()
        history = client.get(f"/api/v1/sessions/{session_id}/events").json()
        assert frame["status"] == "failed"
        assert frame["error_code"] == "stale_frame"
        assert frame["detections"] == []
        assert [event["type"] for event in history] == ["session.state", "analysis.failed"]
        assert history[-1]["payload"]["code"] == "stale_frame"
