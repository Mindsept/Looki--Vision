from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse

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
    latest_only: bool = Form(default=False),
):
    """Accept a timestamped JPEG. Pi must reuse frame_id AND timestamp on retry."""
    runtime = request.app.state.runtime
    try:
        if image.content_type != "image/jpeg":
            raise DomainError(415, "jpeg_required", "Content-Type image/jpeg requis.")
        data = await image.read(runtime.settings.max_image_bytes + 1)
    finally:
        await image.close()
    result, duplicate = await runtime.frames.ingest(
        session_id, frame_id, timestamp, data, latest_only=latest_only
    )
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


@router.post("/sessions/{session_id}/ws-ticket", tags=["Live"])
async def websocket_ticket(session_id: UUID, request: Request):
    runtime = request.app.state.runtime
    await runtime.sessions.get(session_id)
    return {"ticket": runtime.socket_tickets.issue(session_id), "expires_in_seconds": 30}


@router.get(
    "/sessions/{session_id}/frames/{frame_id}/image",
    tags=["Live"],
    response_class=Response,
    responses={
        200: {"content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}}}
    },
)
async def frame_image(session_id: UUID, frame_id: UUID, request: Request):
    image = await request.app.state.runtime.frames.image(session_id, frame_id)
    return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/runtime", tags=["Live"])
async def runtime_info(request: Request):
    settings = request.app.state.runtime.settings
    return {
        "vision_provider": settings.vision_provider,
        "depth_backend": settings.depth_backend,
        "depth_resolution_level": settings.unidepth_resolution_level,
        "device": settings.vision_device,
        "cpu_threads": settings.cpu_threads,
        "object_input_size": settings.yolo_image_size,
        "confidence_threshold": settings.object_confidence_threshold,
        "min_confidence": settings.min_confidence,
        "near_threshold_m": settings.near_threshold_m,
        "far_threshold_m": settings.far_threshold_m,
        "max_frame_age_seconds": settings.max_frame_age_seconds,
        "camera_calibrated": settings.camera_calibration_path is not None,
        "segmentation_tracking": settings.vision_provider == "yolo_seg_track",
        "decoupled_depth": settings.vision_provider == "yolo_seg_track"
        and settings.decoupled_depth,
        "depth_interval_seconds": settings.depth_interval_seconds,
        "depth_max_age_seconds": settings.depth_max_age_seconds,
        "demo_video_available": bool(
            settings.demo_video_path and settings.demo_video_path.is_file()
        ),
    }


@router.get(
    "/demo-video",
    tags=["Live"],
    response_class=FileResponse,
    responses={200: {"content": {"video/mp4": {"schema": {"type": "string", "format": "binary"}}}}},
)
async def demo_video(request: Request):
    path = request.app.state.runtime.settings.demo_video_path
    if path is None or not path.is_file():
        raise DomainError(404, "video_not_configured", "Choisir une vidéo locale dans l'interface.")
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "no-store"})


@router.get("/sessions/{session_id}/frames/{frame_id}/depth", tags=["Live"])
async def frame_depth(session_id: UUID, frame_id: UUID, request: Request):
    from app.repositories.depth import DepthRepository

    runtime = request.app.state.runtime
    await runtime.frames.get(session_id, frame_id)
    async with runtime.db.sessions() as db:
        job = await DepthRepository(db).get_for_frame(session_id, frame_id)
        return {
            "status": job.status if job else "not_sampled",
            "payload": job.result if job else None,
            "error_code": job.error_code if job else None,
        }
