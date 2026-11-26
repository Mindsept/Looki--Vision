from uuid import UUID

from app.core.errors import DomainError
from app.core.time import utcnow
from app.repositories.events import EventRepository
from app.repositories.sessions import SessionRepository
from app.schemas.api import SessionCreate
from app.services.serialization import session_read


async def require_session(repo: SessionRepository, session_id: UUID, active: bool = False):
    session = await repo.get(session_id)
    if session is None:
        raise DomainError(404, "session_not_found", "Session inconnue.")
    if active and session.status != "active":
        raise DomainError(409, "session_closed", "La session est fermée.")
    return session


class SessionService:
    def __init__(self, runtime):
        self.runtime = runtime

    async def create(self, data: SessionCreate):
        context = await self.runtime.centres.context(data.center_id)
        async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
            row = await SessionRepository(db).create(data.center_id, data.user_id, context)
            await EventRepository(db).add(
                row.id, "session.state", {"status": "active", "center_id": row.center_id}
            )
            result = session_read(row)
        return result

    async def get(self, session_id: UUID):
        async with self.runtime.db.sessions() as db:
            return session_read(await require_session(SessionRepository(db), session_id))

    async def close(self, session_id: UUID):
        async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
            row = await require_session(SessionRepository(db), session_id)
            if row.status != "closed":
                row.status = "closed"
                row.closed_at = utcnow()
                await EventRepository(db).add(
                    row.id, "session.state", {"status": "closed", "center_id": row.center_id}
                )
            result = session_read(row)
        await self.runtime.realtime.close_session(session_id)
        return result

    async def events(self, session_id: UUID, after: int, limit: int):
        from app.services.serialization import event_read

        async with self.runtime.db.sessions() as db:
            await require_session(SessionRepository(db), session_id)
            return [
                event_read(row) for row in await EventRepository(db).after(session_id, after, limit)
            ]

    async def live_snapshot(self, session_id: UUID):
        """Bounded viewer bootstrap; resume WSS after cursor without losing a commit."""
        from app.services.serialization import event_read

        # Writers use this same lock: the session, cursor and latest results refer
        # to one committed state, including on PostgreSQL READ COMMITTED.
        async with self.runtime.write_lock, self.runtime.db.sessions() as db:
            session = await require_session(SessionRepository(db), session_id)
            repo = EventRepository(db)
            events = await repo.recent(session_id)
            frame = await repo.latest_of_type(session_id, "frame.analyzed")
            depth = await repo.latest_of_type(session_id, "frame.depth_analyzed")
            return {
                "session": session_read(session),
                "cursor": events[-1].id if events else 0,
                "recent_events": [event_read(row) for row in events],
                "latest_frame": event_read(frame) if frame else None,
                "latest_depth": event_read(depth) if depth else None,
            }
