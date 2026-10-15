from uuid import UUID

from app.core.errors import DomainError
from app.core.time import as_utc, utcnow
from app.repositories.events import EventRepository
from app.repositories.frames import FrameRepository
from app.repositories.sessions import SessionRepository
from app.schemas.api import Question
from app.services.serialization import event_read
from app.services.sessions import require_session


class QuestionService:
    def __init__(self, runtime):
        self.runtime = runtime

    async def answer(self, session_id: UUID, question: Question):
        async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
            session = await require_session(SessionRepository(db), session_id)
            events = EventRepository(db)
            existing = await events.for_request(session_id, question.request_id)
            if existing:
                if existing.payload.get("question") != question.text:
                    raise DomainError(
                        409,
                        "request_id_conflict",
                        "request_id déjà utilisé pour une autre question.",
                    )
                return event_read(existing), True
            if session.status != "active":
                raise DomainError(409, "session_closed", "La session est fermée.")
            frame = await FrameRepository(db).latest_completed(session_id)
            fresh = (
                frame is not None
                and (utcnow() - as_utc(frame.captured_at)).total_seconds()
                <= self.runtime.settings.max_frame_age_seconds
            )
            # No intent inference or invented route: V1 reads the latest observations.
            if fresh:
                text = f"Mode POC, sans assistant génératif. Dernière observation : {frame.description}"
            else:
                text = "Mode POC, sans assistant génératif. Aucune observation récente disponible pour répondre."
            payload = {
                "request_id": str(question.request_id),
                "question": question.text,
                "text": text,
                "mode": "observations_only",
                "center_id": session.center_id,
                "center_name": session.context.get("name"),
                "frame_id": str(frame.frame_id) if fresh else None,
                "simulated": frame.simulated if fresh else None,
            }
            row = await events.add(session_id, "answer", payload, request_id=question.request_id)
            result = event_read(row)
        return result, False
