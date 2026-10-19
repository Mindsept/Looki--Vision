import asyncio
import json
from uuid import UUID

from anyio import CancelScope
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from app.core.auth import token_matches
from app.core.errors import DomainError
from app.core.time import utcnow
from app.schemas.api import Ping, Question, ServerEvent

router = APIRouter()
client_event = TypeAdapter(Question | Ping)


@router.websocket("/api/v1/sessions/{session_id}/ws")
async def session_socket(
    websocket: WebSocket,
    session_id: UUID,
    after: int = Query(default=0, ge=0),
    ticket: str | None = Query(default=None, max_length=128),
):
    runtime = websocket.app.state.runtime
    if not (
        token_matches(websocket.headers.get("authorization"), runtime.settings.api_key)
        or runtime.socket_tickets.consume(ticket, session_id)
    ):
        await websocket.close(code=1008)
        return
    try:
        session = await runtime.sessions.get(session_id)
    except DomainError:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    send_lock = asyncio.Lock()
    cursor = after

    async def send(event: ServerEvent):
        async with send_lock:
            await asyncio.wait_for(websocket.send_json(event.model_dump(mode="json")), timeout=5)

    async def transient(type: str, payload: dict):
        await send(
            ServerEvent(
                id=None, type=type, session_id=session_id, timestamp=utcnow(), payload=payload
            )
        )

    await transient(
        "session.state",
        {
            "status": session.status,
            "center_id": session.center_id,
            "vision_provider": runtime.settings.vision_provider,
        },
    )

    async def publish():
        nonlocal cursor
        while True:
            events = await runtime.sessions.events(session_id, cursor, 100)
            for event in events:
                await send(event)
                cursor = event.id
            if len(events) < 100:
                await asyncio.sleep(runtime.settings.websocket_poll_seconds)

    async def receive():
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            raw = message.get("text")
            if raw is None or len(raw.encode("utf-8")) > 8192:
                await websocket.close(code=1009)
                return
            try:
                event = client_event.validate_python(json.loads(raw))
                if isinstance(event, Ping):
                    await transient("pong", {})
                else:
                    answer, duplicate = await runtime.questions.answer(session_id, event)
                    if duplicate and answer.id <= cursor:
                        # Never overtake earlier undelivered alerts. Unsent answers
                        # go through publish(); replay only already-delivered ids.
                        # Mobile keeps max(last_cursor, event.id) on old duplicates.
                        await send(answer)
            except (ValidationError, ValueError):
                await transient(
                    "error",
                    {
                        "code": "invalid_message",
                        "message": "Événement invalide : question ou ping attendu.",
                    },
                )
            except DomainError as exc:
                await transient("error", {"code": exc.code, "message": exc.message})

    tasks = [asyncio.create_task(publish()), asyncio.create_task(receive())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, TimeoutError, RuntimeError, asyncio.CancelledError):
        pass
    finally:
        with CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await websocket.close()
            except (RuntimeError, WebSocketDisconnect):
                pass
