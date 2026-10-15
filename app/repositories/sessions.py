from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Session


class SessionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, session_id: UUID) -> Session | None:
        return await self.db.get(Session, session_id)

    async def create(self, center_id: str, user_id: str | None, context: dict) -> Session:
        entity = Session(center_id=center_id, user_id=user_id, context=context)
        self.db.add(entity)
        await self.db.flush()
        return entity
