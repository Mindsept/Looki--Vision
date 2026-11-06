"""Durable mobile requests; one process owns provider effects and each media epoch."""

import asyncio
import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from app.core.errors import DomainError
from app.core.time import as_utc, utcnow
from app.models import AudioQuestion, RealtimeCall
from app.providers.lio_prompt import PROMPT_VERSION
from app.providers.realtime import OpenAIRealtimeProvider
from app.repositories.realtime import RealtimeRepository
from app.repositories.sessions import SessionRepository
from app.schemas.mobile import AssistantRead, CallRead, QuestionRead
from app.services.generation_metrics import generation_snapshot, response_transcript, token_usage
from app.services.scene_context import freeze_scene, image_input, snapshot_images
from app.services.sessions import require_session

logger = logging.getLogger(__name__)
TERMINAL = {"completed", "cancelled", "failed", "unavailable"}


def error(code, request_id=None):
    return {
        "code": code,
        "message": {
            "scene_unavailable": "Aucune scène récente exploitable.",
            "backend_restarted": "Connexion interrompue par le redémarrage du backend.",
            "timeout": "Délai dépassé ; négocier un nouvel appel après fermeture.",
            "provider_unavailable": "Service vocal indisponible.",
            "control_unavailable": "Contrôle audio perdu ; fermer la lecture locale.",
            "generation_incomplete": "Réponse interrompue avant sa fin ; ne pas la présenter comme complète.",
        }.get(code, "Service vocal indisponible."),
        "retryable": code != "scene_unavailable",
        "request_id": str(request_id) if request_id else None,
    }


def call_read(row):
    return CallRead(
        session_id=row.session_id,
        call_id=row.id,
        request_id=row.request_id,
        media_epoch=row.media_epoch,
        control_status=row.control_status,
        sdp_answer=row.sdp_answer if row.control_status == "ready" else None,
        error=row.error,
        created_at=as_utc(row.created_at),
    )


def question_read(row):
    frames = (row.context or {}).get("frames", [])
    return QuestionRead(
        session_id=row.session_id,
        request_id=row.request_id,
        call_id=row.call_id,
        media_epoch=row.media_epoch,
        status=row.status,
        context_id=row.context_id,
        transcript=row.transcript,
        error=row.error,
        playback=row.playback,
        media_status=row.media_status,
        updated_at=as_utc(row.updated_at),
        sound_id="v1.scene_unavailable" if row.status == "unavailable" else None,
        generation=row.generation,
        scene={
            "frame_ids": [frame["frame_id"] for frame in frames],
            "frame_count": len(frames),
            "partial": len(frames) < 3,
            "simulated": any(frame["simulated"] for frame in frames),
            "oldest_capture_at": min(frame["captured_at"] for frame in frames),
            "newest_capture_at": max(frame["captured_at"] for frame in frames),
        }
        if frames
        else None,
    )


class RealtimeService:
    def __init__(self, runtime, provider=None):
        self.runtime = runtime
        self.settings = runtime.settings
        self.provider = provider or OpenAIRealtimeProvider(self.settings)
        self.sockets = {}
        self.io_locks = defaultdict(asyncio.Lock)
        self.tasks = {}
        self.close_retry_at = {}
        self.stopping = False

    def spawn(self, key, coroutine):
        if key in self.tasks and not self.tasks[key].done():
            coroutine.close()
            return
        task = asyncio.create_task(coroutine, name=f"looki-realtime-{key[0]}")
        self.tasks[key] = task

        def done(finished):
            if self.tasks.get(key) is finished:
                self.tasks.pop(key, None)
            if not finished.cancelled() and finished.exception():
                logger.error("Realtime task failed: %s", key[0])

        task.add_done_callback(done)

    def available(self):
        if (
            not self.settings.realtime_enabled
            or not self.settings.openai_api_key.get_secret_value()
        ):
            raise DomainError(
                503, "provider_unavailable", "Configurer le service Realtime côté serveur."
            )

    async def configuration(self, session_id):
        async with self.runtime.db.sessions() as db:
            await require_session(SessionRepository(db), session_id)
        return AssistantRead(
            configuration_status="disabled"
            if not self.settings.realtime_enabled
            else (
                "configured" if self.settings.openai_api_key.get_secret_value() else "missing_key"
            ),
            model=self.settings.realtime_model,
            voice=self.settings.realtime_voice,
            prompt_version=PROMPT_VERSION,
            reasoning_effort=self.settings.realtime_reasoning_effort,
            max_output_tokens=self.settings.realtime_max_output_tokens,
            scene_max_age_seconds=self.settings.scene_max_age_seconds,
            question_interval_seconds=self.settings.realtime_question_interval_seconds,
        )

    async def create_call(self, session_id, data):
        offer_hash = hashlib.sha256(data.sdp_offer.encode()).hexdigest()
        async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
            await require_session(SessionRepository(db), session_id)
            repo = RealtimeRepository(db)
            previous = await repo.call_request(session_id, data.request_id)
            if previous:
                if previous.offer_sha256 != offer_hash:
                    raise DomainError(
                        409, "request_conflict", "Cette clé désigne une autre offre SDP."
                    )
                return call_read(previous), True
            await require_session(SessionRepository(db), session_id, active=True)
            self.available()
            if await repo.current_call(session_id):
                raise DomainError(
                    409, "call_active", "Fermer l’appel courant avant une nouvelle offre."
                )
            row = await repo.add(
                RealtimeCall(
                    session_id=session_id,
                    request_id=data.request_id,
                    offer_sha256=offer_hash,
                    media_epoch=await repo.next_epoch(session_id),
                )
            )
            result = call_read(row)
        self.spawn(("open", row.id), self._open(row.id, data.sdp_offer))
        return result, False

    async def _open(self, call_id, offer):
        needs_close = False
        try:
            async with (
                self.io_locks[call_id],
                asyncio.timeout(self.settings.realtime_connect_timeout_seconds),
            ):
                async with self.runtime.db.sessions() as db:
                    call = await db.get(RealtimeCall, call_id)
                    if call.control_status != "connecting":
                        return
                provider_id, answer = await self.provider.create(offer)
                async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
                    call = await db.get(RealtimeCall, call_id)
                    call.provider_call_id, call.sdp_answer = provider_id, answer
                    needs_close = call.control_status != "connecting"
                if not needs_close:
                    socket = await self.provider.attach(provider_id)
                    self.sockets[call_id] = socket
                    async with (
                        self.runtime.write_lock,
                        self.runtime.db.sessions() as db,
                        db.begin(),
                    ):
                        call = await db.get(RealtimeCall, call_id)
                        needs_close = call.control_status != "connecting"
                        if not needs_close:
                            call.control_status = "ready"
                    if not needs_close:
                        self.spawn(("listen", call_id), self._listen(call_id, socket))
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._fail_call(call_id, "provider_unavailable")
            return
        if needs_close:
            self.spawn(("close", call_id), self._finish_close(call_id))

    async def get_call(self, session_id, call_id=None):
        async with self.runtime.db.sessions() as db:
            await require_session(SessionRepository(db), session_id)
            repo = RealtimeRepository(db)
            row = (
                await repo.call(session_id, call_id)
                if call_id
                else await repo.current_call(session_id)
            )
            if row is None:
                raise DomainError(404, "call_not_found", "Appel inconnu.")
            return call_read(row)

    async def submit(self, session_id, data):
        received_at = utcnow()
        async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
            await require_session(SessionRepository(db), session_id)
            repo = RealtimeRepository(db)
            previous = await repo.question(session_id, data.request_id)
            if previous:
                if (previous.text, previous.call_id, previous.media_epoch) != (
                    data.text,
                    data.call_id,
                    data.media_epoch,
                ):
                    raise DomainError(
                        409,
                        "request_conflict",
                        "UUID utilisé pour une autre demande.",
                        request_id=data.request_id,
                    )
                return question_read(previous), True
            await require_session(SessionRepository(db), session_id, active=True)
            call = await repo.call(session_id, data.call_id)
            if call is None:
                raise DomainError(404, "call_not_found", "Appel inconnu.")
            if call.media_epoch != data.media_epoch or call.closed_at is not None:
                raise DomainError(409, "stale_media_epoch", "Époque média périmée.")
            if call.control_status != "ready" or call.id not in self.sockets:
                raise DomainError(409, "call_not_ready", "Contrôle serveur non prêt.")
            if await repo.active_question(session_id):
                raise DomainError(
                    409, "answer_active", "Une demande occupe encore la sortie audio."
                )
            previous_at = await repo.latest_question_at(session_id)
            if previous_at and (received_at - as_utc(previous_at)).total_seconds() < (
                self.settings.realtime_question_interval_seconds
            ):
                raise DomainError(
                    429,
                    "question_rate_limited",
                    "Espacer les nouvelles questions.",
                    request_id=data.request_id,
                )
            context = await freeze_scene(
                db,
                session_id,
                data.request_id,
                data.text,
                received_at,
                self.settings.scene_max_age_seconds,
            )
            row = await repo.add(
                AudioQuestion(
                    session_id=session_id,
                    request_id=data.request_id,
                    call_id=data.call_id,
                    media_epoch=data.media_epoch,
                    text=data.text,
                    created_at=received_at,
                    generation=generation_snapshot(self.settings, received_at),
                    context=context,
                    context_id=UUID(context["context_id"]) if context else None,
                    status="accepted" if context else "unavailable",
                    media_status="reserved" if context else "closed",
                    error=None if context else error("scene_unavailable", data.request_id),
                    deadline_at=received_at
                    + timedelta(seconds=self.settings.realtime_generation_timeout_seconds),
                )
            )
            result = question_read(row)
        if context:
            self.spawn(("generate", row.id), self._generate(row.id))
        return result, False

    async def _generate(self, question_id):
        call_id = None
        try:
            async with self.runtime.db.sessions() as db:
                row = await db.get(AudioQuestion, question_id)
                call_id = row.call_id
                images = await snapshot_images(db, row.session_id, row.context)
            images = await asyncio.to_thread(lambda: [image_input(image) for image in images])
            async with self.io_locks[call_id]:
                async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
                    row = await db.get(AudioQuestion, question_id)
                    call = await db.get(RealtimeCall, call_id)
                    if (
                        row.status != "accepted"
                        or row.media_status == "stopping"
                        or call.control_status != "ready"
                    ):
                        return
                    if any(
                        (utcnow() - datetime.fromisoformat(f["captured_at"])).total_seconds()
                        > self.settings.scene_max_age_seconds
                        for f in row.context["frames"]
                    ):
                        row.status, row.media_status = "unavailable", "closed"
                        row.error, row.updated_at = (
                            error("scene_unavailable", row.request_id),
                            utcnow(),
                        )
                        return
                    row.status, row.updated_at = "generating", utcnow()
                    row.generation = {**row.generation, "submitted_at": row.updated_at.isoformat()}
                # Persist generating before sending. No automatic replay after an uncertain send.
                await self.provider.generate(self.sockets[call_id], row, images)
        except asyncio.CancelledError:
            raise
        except Exception:
            if call_id:
                await self._fail_call(call_id, "provider_unavailable")

    async def get_question(self, session_id, request_id):
        async with self.runtime.db.sessions() as db:
            await require_session(SessionRepository(db), session_id)
            row = await RealtimeRepository(db).question(session_id, request_id)
            if row is None:
                raise DomainError(404, "request_not_found", "Demande inconnue.")
            return question_read(row)

    @staticmethod
    def _identity(row, data):
        if (row.call_id, row.media_epoch) != (data.call_id, data.media_epoch):
            raise DomainError(409, "stale_media_epoch", "La lecture appartient à une autre époque.")

    async def playback(self, session_id, request_id, data):
        close = False
        async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
            row = await RealtimeRepository(db).question(session_id, request_id)
            if row is None:
                raise DomainError(404, "request_not_found", "Demande inconnue.")
            self._identity(row, data)
            snapshot = data.model_dump(exclude={"call_id", "media_epoch"})
            previous = row.playback
            if data.sequence < previous["sequence"]:
                return previous
            if data.sequence == previous["sequence"]:
                if snapshot != previous:
                    raise DomainError(
                        409, "playback_conflict", "Contenu différent pour cette séquence."
                    )
                return previous
            if previous["state"] in {"finished", "interrupted"} or row.media_status in {
                "closed",
                "stopping",
            }:
                return previous
            if data.state == "finished" and row.status != "completed":
                raise DomainError(409, "playback_conflict", "La génération n’est pas terminée.")
            row.playback, row.updated_at = snapshot, utcnow()
            if data.state == "finished":
                row.media_status = "closed"
            elif data.state == "interrupted":
                if row.status not in TERMINAL:
                    row.status = "cancelled"
                row.media_status = "stopping"
                close = True
            else:
                row.media_status = "playing"
            call_id = row.call_id
        if close:
            await self.close_call(session_id, call_id)
        return snapshot

    async def cancel(self, session_id, request_id, data):
        async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
            row = await RealtimeRepository(db).question(session_id, request_id)
            if row is None:
                raise DomainError(404, "request_not_found", "Demande inconnue.")
            self._identity(row, data)
            if row.media_status == "closed":
                return question_read(row), True
            if row.status not in TERMINAL:
                row.status = "cancelled"
            row.media_status, row.updated_at = "stopping", utcnow()
            result = question_read(row)
        await self.close_call(session_id, data.call_id)
        return result, False

    async def close_call(self, session_id, call_id):
        async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
            repo = RealtimeRepository(db)
            call = await repo.call(session_id, call_id)
            if call is None:
                raise DomainError(404, "call_not_found", "Appel inconnu.")
            if call.closed_at is not None:
                return True
            call.control_status = "closing"
            question = await repo.active_question(session_id)
            if question and question.call_id == call_id:
                if question.status not in TERMINAL:
                    question.status = "cancelled"
                question.media_status, question.updated_at = "stopping", utcnow()
        self.spawn(("close", call_id), self._finish_close(call_id))
        return False

    async def _finish_close(self, call_id):
        try:
            async with (
                self.io_locks[call_id],
                asyncio.timeout(self.settings.realtime_connect_timeout_seconds),
            ):
                async with self.runtime.db.sessions() as db:
                    call = await db.get(RealtimeCall, call_id)
                    if call.closed_at is not None:
                        return
                    question = await RealtimeRepository(db).active_question(call.session_id)
                socket = self.sockets.get(call_id)
                if socket:
                    try:
                        await self.provider.interrupt(
                            socket, question.provider_response_id if question else None
                        )
                    except Exception:
                        pass  # Hangup remains the authoritative fallback.
                if call.provider_call_id:
                    self.available()
                    await self.provider.hangup(call.provider_call_id)
                if socket:
                    await socket.close()
                    self.sockets.pop(call_id, None)
                async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
                    call = await db.get(RealtimeCall, call_id)
                    call.control_status, call.closed_at, call.sdp_answer = "closed", utcnow(), None
                    self.close_retry_at.pop(call_id, None)
                    question = await RealtimeRepository(db).active_question(call.session_id)
                    if question and question.call_id == call_id:
                        question.media_status, question.updated_at = "closed", utcnow()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.close_retry_at[call_id] = utcnow() + timedelta(seconds=5)
            async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
                call = await db.get(RealtimeCall, call_id)
                call.control_status, call.error = "failed", error("control_unavailable")

    async def _listen(self, call_id, socket):
        try:
            async for raw in socket:
                event = json.loads(raw)
                await self.handle_event(call_id, event)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if not self.stopping:
            async with self.runtime.db.sessions() as db:
                call = await db.get(RealtimeCall, call_id)
                lost = call.control_status == "ready"
            if lost:
                await self._fail_call(call_id, "control_unavailable")

    async def handle_event(self, call_id, event):
        kind, failed = event.get("type"), False
        failure_code = "provider_unavailable"
        if kind not in {
            "response.created",
            "response.done",
            "response.output_audio_transcript.done",
            "output_audio_buffer.started",
            "error",
        }:
            return  # Audio bytes never leave this adapter as LOOKi payloads.
        async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
            call = await db.get(RealtimeCall, call_id)
            if call.control_status != "ready":
                return
            row = await RealtimeRepository(db).active_question(call.session_id)
            response = event.get("response", {})
            if kind == "response.created":
                if (
                    row is None
                    or (response.get("metadata") or {}).get("request_id") != str(row.request_id)
                    or row.status != "generating"
                ):
                    failed = True  # Unexpected generation from a client data channel.
                elif row.provider_response_id not in {None, response.get("id")}:
                    failed = True
                else:
                    row.provider_response_id = response["id"]
                    row.generation = {
                        **(row.generation or {}),
                        "provider_created_at": utcnow().isoformat(),
                    }
            elif kind == "error":
                failed = True
            elif (
                row and (event.get("response_id") or response.get("id")) == row.provider_response_id
            ):
                if kind == "response.output_audio_transcript.done":
                    row.transcript = str(event.get("transcript", ""))[:8000]
                elif kind == "output_audio_buffer.started":
                    if row.generation and not row.generation.get("provider_audio_started_at"):
                        row.generation = {
                            **row.generation,
                            "provider_audio_started_at": utcnow().isoformat(),
                        }
                elif kind == "response.done" and row.status not in TERMINAL:
                    provider_status = response.get("status")
                    row.generation = {
                        **(row.generation or {}),
                        "finished_at": utcnow().isoformat(),
                        "provider_status": provider_status
                        if provider_status in {"completed", "cancelled", "failed", "incomplete"}
                        else "failed",
                        "usage": token_usage(response.get("usage")),
                    }
                    row.transcript = response_transcript(response) or row.transcript
                    row.status = {"completed": "completed", "cancelled": "cancelled"}.get(
                        provider_status, "failed"
                    )
                    row.deadline_at = utcnow() + timedelta(
                        seconds=self.settings.realtime_playback_timeout_seconds
                    )
                    if row.status != "completed":
                        failed = True
                        if provider_status == "incomplete":
                            failure_code = "generation_incomplete"
                row.updated_at = utcnow()
        if failed:
            await self._fail_call(call_id, failure_code)

    async def _fail_call(self, call_id, code):
        async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
            call = await db.get(RealtimeCall, call_id)
            if call is None or call.closed_at is not None:
                return
            call.control_status, call.error = "failed", error(code)
            row = await RealtimeRepository(db).active_question(call.session_id)
            if row and row.call_id == call_id:
                if row.status not in TERMINAL:
                    row.status = "failed"
                row.error, row.media_status, row.updated_at = (
                    error(code, row.request_id),
                    "stopping",
                    utcnow(),
                )
        self.spawn(("close", call_id), self._finish_close(call_id))

    async def close_session(self, session_id):
        async with self.runtime.db.sessions() as db:
            call = await RealtimeRepository(db).current_call(session_id)
        if call:
            await self.close_call(session_id, call.id)

    async def recover(self):
        async with self.runtime.db.sessions() as db:
            ids = list(
                await db.scalars(select(RealtimeCall.id).where(RealtimeCall.closed_at.is_(None)))
            )
        for call_id in ids:
            await self._fail_call(call_id, "backend_restarted")

    async def watchdog(self):
        while True:
            await asyncio.sleep(1)
            now = utcnow()
            async with self.runtime.db.sessions() as db:
                calls = list(
                    await db.scalars(select(RealtimeCall).where(RealtimeCall.closed_at.is_(None)))
                )
                overdue = set(
                    await db.scalars(
                        select(AudioQuestion.call_id).where(
                            AudioQuestion.media_status != "closed", AudioQuestion.deadline_at < now
                        )
                    )
                )
            for call in calls:
                if call.control_status in {"failed", "closing"}:
                    if now >= self.close_retry_at.get(call.id, now):
                        self.spawn(("close", call.id), self._finish_close(call.id))
                elif (
                    call.id in overdue
                    or (now - as_utc(call.created_at)).total_seconds()
                    > self.settings.realtime_call_max_seconds
                ):
                    await self._fail_call(call.id, "timeout")

    async def shutdown(self):
        self.stopping = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        async with self.runtime.db.sessions() as db:
            calls = list(
                await db.scalars(select(RealtimeCall).where(RealtimeCall.closed_at.is_(None)))
            )
        for call in calls:
            await self._fail_call(call.id, "backend_restarted")
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        await self.provider.close()
