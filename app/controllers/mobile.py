from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.core.auth import issue_session_token, require_auth
from app.schemas.mobile import (
    AccessTokens,
    AssistantRead,
    CallCreate,
    CallRead,
    CancelRequest,
    ErrorEnvelope,
    MobileQuestion,
    PlaybackRead,
    PlaybackUpdate,
    QuestionRead,
)

router = APIRouter(
    prefix="/api/v1/sessions/{session_id}",
    dependencies=[Depends(require_auth)],
    tags=["Mobile HTTP"],
    responses={code: {"model": ErrorEnvelope} for code in (401, 403, 404, 409, 422, 429, 503, 504)},
)


@router.get("/assistant", response_model=AssistantRead)
async def assistant_configuration(session_id: UUID, request: Request, response: Response):
    """Local configuration only. This read neither contacts OpenAI nor starts generation."""
    response.headers["Cache-Control"] = "no-store"
    return await request.app.state.runtime.realtime.configuration(session_id)


@router.post("/access-tokens", response_model=AccessTokens)
async def access_tokens(session_id: UUID, request: Request, response: Response):
    """Operator-only provisioning. Deliver each capability to its intended device out of band."""
    runtime = request.app.state.runtime
    session = await runtime.sessions.get(session_id)
    if session.status != "active":
        from app.core.errors import DomainError

        raise DomainError(409, "session_closed", "La session est fermée.")
    ttl = runtime.settings.session_token_ttl_seconds
    response.headers["Cache-Control"] = "no-store"
    return AccessTokens(
        session_id=session_id,
        expires_in_seconds=ttl,
        mobile_token=issue_session_token(runtime.settings.api_key, session_id, "mobile", ttl),
        pi_token=issue_session_token(runtime.settings.api_key, session_id, "pi", ttl),
    )


@router.post(
    "/realtime/calls",
    response_model=CallRead,
    status_code=201,
    responses={200: {"model": CallRead, "description": "Same offer already registered"}},
)
async def create_call(session_id: UUID, data: CallCreate, request: Request, response: Response):
    """Register once, then poll until ready to obtain the SDP answer. No generation on connect."""
    result, duplicate = await request.app.state.runtime.realtime.create_call(session_id, data)
    if duplicate:
        response.status_code = 200
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/realtime/calls/current", response_model=CallRead)
async def current_call(session_id: UUID, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await request.app.state.runtime.realtime.get_call(session_id)


@router.get("/realtime/calls/{call_id}", response_model=CallRead)
async def get_call(session_id: UUID, call_id: UUID, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await request.app.state.runtime.realtime.get_call(session_id, call_id)


@router.delete(
    "/realtime/calls/{call_id}",
    status_code=204,
    responses={202: {"description": "Closing; poll call state"}},
)
async def close_call(session_id: UUID, call_id: UUID, request: Request):
    closed = await request.app.state.runtime.realtime.close_call(session_id, call_id)
    return Response(status_code=204 if closed else 202)


@router.post(
    "/questions",
    response_model=QuestionRead,
    status_code=202,
    responses={
        200: {"model": QuestionRead, "description": "Same logical request; no new generation"}
    },
)
async def ask_question(
    session_id: UUID, data: MobileQuestion, request: Request, response: Response
):
    result, duplicate = await request.app.state.runtime.realtime.submit(session_id, data)
    if duplicate:
        response.status_code = 200
    return result


@router.get("/questions/{request_id}", response_model=QuestionRead)
async def get_question(session_id: UUID, request_id: UUID, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await request.app.state.runtime.realtime.get_question(session_id, request_id)


@router.put("/questions/{request_id}/playback", response_model=PlaybackRead)
async def playback(session_id: UUID, request_id: UUID, data: PlaybackUpdate, request: Request):
    return await request.app.state.runtime.realtime.playback(session_id, request_id, data)


@router.post(
    "/questions/{request_id}/cancel",
    response_model=QuestionRead,
    status_code=202,
    responses={200: {"model": QuestionRead, "description": "Media already closed"}},
)
async def cancel(
    session_id: UUID, request_id: UUID, data: CancelRequest, request: Request, response: Response
):
    result, closed = await request.app.state.runtime.realtime.cancel(session_id, request_id, data)
    if closed:
        response.status_code = 200
    return result
