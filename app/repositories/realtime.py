from sqlalchemy import func, select

from app.models import AudioQuestion, RealtimeCall


class RealtimeRepository:
    def __init__(self, db):
        self.db = db

    async def call(self, session_id, call_id):
        return await self.db.scalar(
            select(RealtimeCall).where(
                RealtimeCall.session_id == session_id, RealtimeCall.id == call_id
            )
        )

    async def call_request(self, session_id, request_id):
        return await self.db.scalar(
            select(RealtimeCall).where(
                RealtimeCall.session_id == session_id, RealtimeCall.request_id == request_id
            )
        )

    async def current_call(self, session_id):
        return await self.db.scalar(
            select(RealtimeCall).where(
                RealtimeCall.session_id == session_id, RealtimeCall.closed_at.is_(None)
            )
        )

    async def next_epoch(self, session_id):
        return 1 + (
            await self.db.scalar(
                select(func.max(RealtimeCall.media_epoch)).where(
                    RealtimeCall.session_id == session_id
                )
            )
            or 0
        )

    async def question(self, session_id, request_id):
        return await self.db.scalar(
            select(AudioQuestion).where(
                AudioQuestion.session_id == session_id, AudioQuestion.request_id == request_id
            )
        )

    async def active_question(self, session_id):
        return await self.db.scalar(
            select(AudioQuestion).where(
                AudioQuestion.session_id == session_id, AudioQuestion.media_status != "closed"
            )
        )

    async def add(self, row):
        self.db.add(row)
        await self.db.flush()
        return row
