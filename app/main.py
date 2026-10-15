import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.controllers.http import router as http_router
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
            )
            if settings.worker_enabled:
                await runtime.worker.recover()
                runtime.worker_task = asyncio.create_task(
                    runtime.worker.run(), name="looki-analysis-worker"
                )
            yield
        finally:
            if runtime.worker_task:
                runtime.worker_task.cancel()
                await asyncio.gather(runtime.worker_task, return_exceptions=True)
            await runtime.db.dispose()

    application = FastAPI(
        title="LOOKi Backend",
        version="0.1.0",
        lifespan=lifespan,
        description="POC : ingestion JPEG, proximité et événements iPhone. Un processus serveur par déploiement.",
    )
    application.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_request_bytes)

    @application.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers={"Retry-After": "1"} if exc.status == 429 else None,
        )

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
            return {"status": "ready", "vision_provider": settings.vision_provider}
        except Exception:
            return JSONResponse(status_code=503, content={"status": "unavailable"})

    application.include_router(http_router)
    application.include_router(websocket_router)
    return application


app = create_app()
