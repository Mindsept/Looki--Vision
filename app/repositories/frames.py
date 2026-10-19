from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.models import Detection, Frame


class FrameRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, session_id: UUID, frame_id: UUID) -> Frame | None:
        return await self.db.scalar(
            select(Frame).where(Frame.session_id == session_id, Frame.frame_id == frame_id)
        )

    async def get_internal(self, internal_id: UUID) -> Frame | None:
        return await self.db.get(Frame, internal_id)

    async def pending_count(self) -> int:
        return (
            await self.db.scalar(
                select(func.count())
                .select_from(Frame)
                .where(Frame.status.in_(["pending", "processing"]))
            )
            or 0
        )

    async def create(self, **fields) -> Frame:
        entity = Frame(**fields)
        self.db.add(entity)
        await self.db.flush()
        return entity

    async def claim_next(self) -> Frame | None:
        statement = (
            select(Frame)
            .where(Frame.status == "pending")
            .order_by(Frame.received_at, Frame.id)
            .limit(1)
            .options(undefer(Frame.image))
            .with_for_update(skip_locked=True)
        )
        entity = await self.db.scalar(statement)
        if entity:
            entity.status = "processing"
            await self.db.flush()
        return entity

    async def reset_interrupted(self) -> int:
        result = await self.db.execute(
            update(Frame).where(Frame.status == "processing").values(status="pending")
        )
        return result.rowcount

    async def detections(self, internal_id: UUID) -> list[Detection]:
        return list(
            await self.db.scalars(
                select(Detection).where(Detection.frame_id == internal_id).order_by(Detection.id)
            )
        )

    async def latest_completed(self, session_id: UUID) -> Frame | None:
        return await self.db.scalar(
            select(Frame)
            .where(Frame.session_id == session_id, Frame.status == "completed")
            .order_by(Frame.captured_at.desc(), Frame.received_at.desc())
            .limit(1)
        )

    async def pending_for_session(self, session_id: UUID) -> list[Frame]:
        return list(
            await self.db.scalars(
                select(Frame).where(Frame.session_id == session_id, Frame.status == "pending")
            )
        )

    async def image_bytes(self, session_id: UUID, frame_id: UUID) -> bytes | None:
        return await self.db.scalar(
            select(Frame.image).where(Frame.session_id == session_id, Frame.frame_id == frame_id)
        )

    async def latest_capture(self, session_id: UUID):
        return await self.db.scalar(
            select(func.max(Frame.captured_at)).where(Frame.session_id == session_id)
        )
