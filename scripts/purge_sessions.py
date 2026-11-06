#!/usr/bin/env python3
"""Administrative purge of old closed sessions. Dry run by default; stop the backend first."""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.core.database import Database  # noqa: E402
from app.models import AudioQuestion, RealtimeCall  # noqa: E402
from app.models.entities import DepthJob, Detection, Event, Frame, Session  # noqa: E402


async def purge(db, before, apply=False):
    if before.tzinfo is None:
        raise ValueError("La date doit inclure un fuseau horaire.")
    async with db.sessions() as connection, connection.begin():
        # Closed sessions with unfinished work/media are excluded, even during maintenance.
        busy_frames = select(Frame.session_id).where(Frame.status.in_(["pending", "processing"]))
        busy_depth = (
            select(Frame.session_id)
            .join(DepthJob)
            .where(DepthJob.status.in_(["pending", "processing"]))
        )
        calls = select(RealtimeCall.session_id).where(RealtimeCall.closed_at.is_(None))
        media = select(AudioQuestion.session_id).where(AudioQuestion.media_status != "closed")
        ids = list(
            await connection.scalars(
                select(Session.id)
                .where(
                    Session.status == "closed",
                    Session.closed_at < before,
                    Session.id.not_in(busy_frames),
                    Session.id.not_in(busy_depth),
                    Session.id.not_in(calls),
                    Session.id.not_in(media),
                )
                .with_for_update()
            )
        )
        frame_ids = select(Frame.id).where(Frame.session_id.in_(ids))
        predicates = [
            (AudioQuestion, AudioQuestion.session_id.in_(ids)),
            (RealtimeCall, RealtimeCall.session_id.in_(ids)),
            (Event, Event.session_id.in_(ids)),
            (Detection, Detection.frame_id.in_(frame_ids)),
            (DepthJob, DepthJob.frame_id.in_(frame_ids)),
            (Frame, Frame.session_id.in_(ids)),
            (Session, Session.id.in_(ids)),
        ]
        counts = {}
        for model, predicate in predicates:
            counts[model.__tablename__] = await connection.scalar(
                select(func.count()).select_from(model).where(predicate)
            )
            if apply:
                await connection.execute(delete(model).where(predicate))
        return {
            "applied": apply,
            "before": before.isoformat(),
            "counts": counts,
            "backups_purged": False,
        }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--before",
        required=True,
        type=datetime.fromisoformat,
        help="Explicit retention cutoff, RFC3339 with timezone.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete eligible rows. Stop the backend before applying.",
    )
    args = parser.parse_args()
    if args.before.tzinfo is None:
        parser.error("--before doit inclure le fuseau horaire (par exemple +00:00).")
    db = Database(Settings())
    try:
        print(json.dumps(await purge(db, args.before.astimezone(UTC), args.apply), indent=2))
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
