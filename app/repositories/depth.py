from sqlalchemy import select, update
from sqlalchemy.orm import undefer

from app.models.entities import DepthJob, Frame


class DepthRepository:
    def __init__(self, db):
        self.db = db

    async def claim_next(self):
        job = await self.db.scalar(
            select(DepthJob)
            .where(DepthJob.status == "pending")
            .order_by(DepthJob.created_at)
            .limit(1)
            .options(undefer(DepthJob.masks))
            .with_for_update(skip_locked=True)
        )
        if job:
            job.status = "processing"
            await self.db.flush()
        return job

    async def recover(self):
        await self.db.execute(
            update(DepthJob).where(DepthJob.status == "processing").values(status="pending")
        )
        return await self.db.scalar(
            select(DepthJob.id).where(DepthJob.status == "pending").limit(1)
        )

    async def get_for_frame(self, session_id, frame_id):
        return await self.db.scalar(
            select(DepthJob)
            .join(Frame, DepthJob.frame_id == Frame.id)
            .where(Frame.session_id == session_id, Frame.frame_id == frame_id)
        )
