from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile

from app.core.auth import require_auth
from app.core.errors import DomainError
from app.schemas.api import FrameRead, ServerEvent, SessionCreate, SessionRead

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_auth)])


@router.post("/sessions", response_model=SessionRead, status_code=201, tags=["Sessions"])
async def create_session(data: SessionCreate, request: Request):
    """Create a session and snapshot the centre context."""
    return await request.app.state.runtime.sessions.create(data)


@router.get("/sessions/{session_id}", response_model=SessionRead, tags=["Sessions"])
async def get_session(session_id: UUID, request: Request):
    return await request.app.state.runtime.sessions.get(session_id)


@router.post("/sessions/{session_id}/close", response_model=SessionRead, tags=["Sessions"])
async def close_session(session_id: UUID, request: Request):
    """Idempotently close admission; already accepted frames finish processing."""
    return await request.app.state.runtime.sessions.close(session_id)


@router.post(
    "/sessions/{session_id}/frames",
    response_model=FrameRead,
    status_code=202,
    responses={
        200: {"model": FrameRead, "description": "Exact retry of an existing frame"},
        409: {"description": "Closed session or conflicting frame_id"},
        413: {"description": "Image or request exceeds configured limits"},
        415: {"description": "JPEG required"},
        429: {"description": "Analysis queue full; Retry-After: 1"},
    },
    tags=["Frames"],
)
async def ingest_frame(
    session_id: UUID,
    request: Request,
    response: Response,
    frame_id: UUID = Form(...),
    timestamp: datetime = Form(...),
    image: UploadFile = File(...),
):
    """Accept a timestamped JPEG. Pi must reuse frame_id AND timestamp on retry."""
    runtime = request.app.state.runtime
    try:
        if image.content_type != "image/jpeg":
            raise DomainError(415, "jpeg_required", "Content-Type image/jpeg requis.")
        data = await image.read(runtime.settings.max_image_bytes + 1)
    finally:
        await image.close()
    result, duplicate = await runtime.frames.ingest(session_id, frame_id, timestamp, data)
    if duplicate:
        response.status_code = 200
    return result


@router.get("/sessions/{session_id}/frames/{frame_id}", response_model=FrameRead, tags=["Frames"])
async def get_frame(session_id: UUID, frame_id: UUID, request: Request):
    return await request.app.state.runtime.frames.get(session_id, frame_id)


@router.get("/sessions/{session_id}/events", response_model=list[ServerEvent], tags=["Events"])
async def get_events(
    session_id: UUID,
    request: Request,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    return await request.app.state.runtime.sessions.events(session_id, after, limit)
