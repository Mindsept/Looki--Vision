import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException

from app.controllers.http import router as http_router
from app.controllers.mobile import router as mobile_router
from app.controllers.websocket import router as websocket_router
from app.core.config import Settings
from app.core.database import Base
from app.core.errors import DomainError
from app.core.middleware import BodyLimitMiddleware
from app.core.runtime import Runtime
from app.vision import create_provider


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application):
        runtime = Runtime(settings)
        application.state.runtime = runtime
        logging.basicConfig(level=logging.INFO)
        try:
            if settings.create_schema:
                async with runtime.db.engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
            runtime.vision = await asyncio.to_thread(
                create_provider,
                settings.vision_provider,
                settings.depth_model_id,
                settings.vision_device,
                object_model_id=settings.object_model_id,
                object_confidence_threshold=settings.object_confidence_threshold,
                depth_backend=settings.depth_backend,
                camera_calibration_path=settings.camera_calibration_path,
                **(
                    {"unidepth_resolution_level": settings.unidepth_resolution_level}
                    if settings.unidepth_resolution_level is not None
                    else {}
                ),
                **(
                    {
                        "yolo_image_size": settings.yolo_image_size,
                        "cpu_threads": settings.cpu_threads,
                    }
                    if settings.vision_provider in {"yolo_objects", "yolo_seg_track"}
                    else {}
                ),
            )
            await runtime.realtime.recover()
            runtime.realtime_task = asyncio.create_task(
                runtime.realtime.watchdog(), name="looki-realtime-watchdog"
            )
            if settings.worker_enabled:
                await runtime.worker.recover()
                runtime.worker_task = asyncio.create_task(
                    runtime.worker.run(), name="looki-analysis-worker"
                )
                if settings.vision_provider == "yolo_seg_track" and settings.decoupled_depth:
                    await runtime.depth_worker.recover()
                    runtime.depth_task = asyncio.create_task(
                        runtime.depth_worker.run(), name="looki-depth-worker"
                    )
            yield
        finally:
            if runtime.realtime_task:
                runtime.realtime_task.cancel()
                await asyncio.gather(runtime.realtime_task, return_exceptions=True)
            await runtime.realtime.shutdown()
            if runtime.worker_task:
                runtime.worker_task.cancel()
                await asyncio.gather(runtime.worker_task, return_exceptions=True)
            if runtime.depth_task:
                runtime.depth_task.cancel()
                await asyncio.gather(runtime.depth_task, return_exceptions=True)
            await runtime.db.dispose()

    application = FastAPI(
        title="LOOKi Backend",
        version="0.1.0",
        lifespan=lifespan,
        description="POC : ingestion PNG/JPEG, alertes WSS, questions HTTP et contrôle Realtime. Un processus serveur par déploiement.",
    )
    application.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_request_bytes)

    @application.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.status in {429, 502, 503, 504} or exc.code == "call_not_ready",
                    "request_id": exc.request_id,
                }
            },
            headers={"Retry-After": "1"}
            if exc.status == 429
            else {"WWW-Authenticate": "Bearer"}
            if exc.status == 401
            else None,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Never echo a question, SDP, token or other submitted secret in error bodies.
        code = (
            "invalid_sdp"
            if any("sdp_offer" in e["loc"] for e in exc.errors())
            else "invalid_request"
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": code,
                    "message": "Champs de requête invalides.",
                    "retryable": False,
                    "request_id": None,
                }
            },
        )

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if (
            exc.status_code == 413
            and isinstance(exc.detail, dict)
            and exc.detail.get("code") == "request_too_large"
        ):
            return await domain_error_handler(
                request, DomainError(413, "request_too_large", "Requête trop volumineuse.")
            )
        return await http_exception_handler(request, exc)

    @application.get("/health/live", tags=["Health"])
    async def live():
        return {"status": "alive"}

    @application.get("/health/ready", tags=["Health"])
    async def ready(request: Request):
        runtime = request.app.state.runtime
        try:
            async with runtime.db.sessions() as db:
                await db.execute(text("SELECT 1"))
            if runtime.vision is None or (
                settings.worker_enabled
                and (runtime.worker_task is None or runtime.worker_task.done())
            ):
                raise RuntimeError("Worker unavailable")
            if (
                settings.worker_enabled
                and settings.vision_provider == "yolo_seg_track"
                and settings.decoupled_depth
                and (runtime.depth_task is None or runtime.depth_task.done())
            ):
                raise RuntimeError("Depth worker unavailable")
            return {"status": "ready", "vision_provider": settings.vision_provider}
        except Exception:
            return JSONResponse(status_code=503, content={"status": "unavailable"})

    application.include_router(http_router)
    application.include_router(mobile_router)
    application.include_router(websocket_router)
    static = Path(__file__).resolve().parent / "static"
    if static.is_dir():
        application.mount("/live", StaticFiles(directory=static, html=True), name="live")

        @application.get("/", include_in_schema=False)
        async def home():
            return RedirectResponse("/live/")

    return application


app = create_app()
