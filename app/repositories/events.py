from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event


class EventRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def add(
        self,
        session_id: UUID,
        type: str,
        payload: dict,
        *,
        frame_id: UUID | None = None,
        request_id: UUID | None = None,
    ) -> Event:
        # Every event-producing transaction holds Runtime.write_lock until commit.
        # This guarantees commit order equals cursor order in the single-process POC.
        entity = Event(
            session_id=session_id,
            frame_id=frame_id,
            type=type,
            payload=payload,
            request_id=request_id,
        )
        self.db.add(entity)
        await self.db.flush()
        return entity

    async def after(self, session_id: UUID, cursor: int, limit: int = 100) -> list[Event]:
        return list(
            await self.db.scalars(
                select(Event)
                .where(Event.session_id == session_id, Event.id > cursor)
                .order_by(Event.id)
                .limit(limit)
            )
        )

    async def for_request(self, session_id: UUID, request_id: UUID) -> Event | None:
        return await self.db.scalar(
            select(Event).where(Event.session_id == session_id, Event.request_id == request_id)
        )

    async def recent(self, session_id: UUID, limit: int = 100) -> list[Event]:
        rows = list(
            await self.db.scalars(
                select(Event)
                .where(Event.session_id == session_id)
                .order_by(Event.id.desc())
                .limit(limit)
            )
        )
        return list(reversed(rows))

    async def latest_of_type(self, session_id: UUID, event_type: str) -> Event | None:
        return await self.db.scalar(
            select(Event)
            .where(Event.session_id == session_id, Event.type == event_type)
            .order_by(Event.id.desc())
            .limit(1)
        )
