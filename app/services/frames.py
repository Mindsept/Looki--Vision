import asyncio
import hashlib
import io
import warnings
from datetime import datetime
from uuid import UUID

from PIL import Image, UnidentifiedImageError

from app.core.errors import DomainError
from app.core.time import as_utc, utcnow
from app.repositories.events import EventRepository
from app.repositories.frames import FrameRepository
from app.repositories.sessions import SessionRepository
from app.services.serialization import frame_read
from app.services.sessions import require_session


def inspect_image(data: bytes, max_pixels: int, content_type: str | None) -> tuple[int, int, str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"JPEG", "PNG"}:
                    raise DomainError(
                        415, "unsupported_image_type", "Une image PNG ou JPEG est requise."
                    )
                detected_type = Image.MIME[image.format]
                if content_type is not None and content_type != detected_type:
                    raise DomainError(415, "image_type_mismatch", "MIME et format réel différents.")
                if image.format == "PNG" and (
                    image.mode != "RGB" or image.n_frames != 1 or data[24] != 8
                ):
                    raise DomainError(422, "invalid_png", "PNG RGB 8 bits non animé requis.")
                if image.width * image.height > max_pixels:
                    raise DomainError(
                        413, "image_dimensions_exceeded", "Image trop grande en pixels."
                    )
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()  # JPEG verify() alone does not catch every truncated pixel stream.
                return *image.size, detected_type
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise DomainError(422, "invalid_image", "Image PNG ou JPEG invalide ou tronquée.") from exc
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise DomainError(413, "image_dimensions_exceeded", "Image trop grande en pixels.") from exc


class FrameService:
    def __init__(self, runtime):
        self.runtime = runtime

    async def ingest(
        self,
        session_id: UUID,
        frame_id: UUID,
        timestamp: datetime,
        image: bytes,
        *,
        latest_only: bool = False,
        content_type: str | None = None,
    ):
        settings = self.runtime.settings
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise DomainError(422, "timezone_required", "timestamp doit inclure un fuseau horaire.")
        timestamp = as_utc(timestamp)
        if len(image) > settings.max_image_bytes:
            raise DomainError(413, "image_too_large", "Image trop volumineuse.")
        checksum = hashlib.sha256(image).hexdigest()
        # Fast idempotency lookup before freshness validation: an exact retry remains valid later.
        async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
            await require_session(SessionRepository(db), session_id)
            existing = await FrameRepository(db).get(session_id, frame_id)
            if existing:
                self._check_retry(existing, checksum, timestamp)
                if content_type is not None and content_type != existing.content_type:
                    raise DomainError(415, "image_type_mismatch", "MIME et format réel différents.")
                return frame_read(existing, await FrameRepository(db).detections(existing.id)), True
        width, height, detected_type = await asyncio.to_thread(
            inspect_image, image, settings.max_image_pixels, content_type
        )
        async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
            # Recheck inside the atomic admission section after decoding outside the lock.
            session = await require_session(SessionRepository(db), session_id)
            repo = FrameRepository(db)
            existing = await repo.get(session_id, frame_id)
            if existing:
                self._check_retry(existing, checksum, timestamp)
                return frame_read(existing, await repo.detections(existing.id)), True
            if session.status != "active":
                raise DomainError(409, "session_closed", "La session est fermée.")
            age = (utcnow() - timestamp).total_seconds()
            if age > settings.max_frame_age_seconds or age < -settings.max_future_seconds:
                raise DomainError(
                    422, "invalid_timestamp", "Image périmée ou horodatage dans le futur."
                )
            if latest_only:
                latest_capture = await repo.latest_capture(session_id)
                if latest_capture is not None and as_utc(latest_capture) >= timestamp:
                    raise DomainError(
                        409, "out_of_order_frame", "Une capture plus récente a déjà été reçue."
                    )
                for pending in await repo.pending_for_session(session_id):
                    pending.status = "failed"
                    pending.error_code = "superseded_frame"
                    pending.processed_at = utcnow()
                    await EventRepository(db).add(
                        session_id,
                        "analysis.failed",
                        {
                            "frame_id": str(pending.frame_id),
                            "code": "superseded_frame",
                            "text": "Image remplacée avant analyse par une capture plus récente.",
                        },
                        frame_id=pending.id,
                    )
                await db.flush()
            if await repo.pending_count() >= settings.max_pending_frames:
                raise DomainError(
                    429, "queue_full", "File d'analyse pleine. Réessayer après une seconde."
                )
            row = await repo.create(
                session_id=session_id,
                frame_id=frame_id,
                captured_at=timestamp,
                sha256=checksum,
                image=image,
                width=width,
                height=height,
                content_type=detected_type,
            )
            result = frame_read(row, [])
        return result, False

    @staticmethod
    def _check_retry(row, checksum, timestamp):
        if row.sha256 != checksum or as_utc(row.captured_at) != timestamp:
            raise DomainError(
                409,
                "frame_id_conflict",
                "frame_id déjà utilisé avec un contenu ou timestamp différent.",
            )

    async def get(self, session_id: UUID, frame_id: UUID):
        async with self.runtime.db.sessions() as db:
            await require_session(SessionRepository(db), session_id)
            repo = FrameRepository(db)
            row = await repo.get(session_id, frame_id)
            if row is None:
                raise DomainError(404, "frame_not_found", "Image inconnue.")
            return frame_read(row, await repo.detections(row.id))

    async def image(self, session_id: UUID, frame_id: UUID):
        async with self.runtime.db.sessions() as db:
            await require_session(SessionRepository(db), session_id)
            image = await FrameRepository(db).image_bytes(session_id, frame_id)
            if image is None:
                raise DomainError(404, "frame_not_found", "Image inconnue.")
            row = await FrameRepository(db).get(session_id, frame_id)
            return image, row.content_type
