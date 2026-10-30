import asyncio
import io
import logging
import math
from dataclasses import replace
from time import perf_counter

from PIL import Image

from app.core.time import as_utc, utcnow
from app.models import Detection
from app.models.entities import DepthJob
from app.repositories.events import EventRepository
from app.repositories.frames import FrameRepository
from app.services.perception import RULES, PerceptionService
from app.services.proximity import ProximityService
from app.services.serialization import detection_read

logger = logging.getLogger(__name__)


class AnalysisWorker:
    """A single durable consumer. Inference runs off the event loop, one frame at a time."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.proximity = ProximityService(runtime.settings)
        self.perception = PerceptionService(runtime.settings)
        self.decoupled = (
            runtime.settings.vision_provider == "yolo_seg_track"
            and runtime.settings.decoupled_depth
        )

    async def recover(self):
        async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
            count = await FrameRepository(db).reset_interrupted()
            if count:
                logger.info("Recovered %s interrupted frames", count)

    async def run(self):
        needs_recovery = False
        while True:
            try:
                if needs_recovery:
                    await self.recover()
                    needs_recovery = False
                async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
                    frame = await FrameRepository(db).claim_next()
                if frame is None:
                    await asyncio.sleep(self.runtime.settings.worker_poll_seconds)
                    continue
                await self.process(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Retry an uncommitted in-flight frame when the database returns.
                # There is one consumer, so resetting processing rows is safe here.
                needs_recovery = True
                logger.exception("Analysis worker iteration failed")
                await asyncio.sleep(max(1.0, self.runtime.settings.worker_poll_seconds))

    def _analyze(self, image_bytes):
        with Image.open(io.BytesIO(image_bytes)) as image:
            if self.decoupled:
                return self.runtime.vision.analyze_visual(
                    image.convert("RGB"), include_masks=self.runtime.depth_worker.due
                )
            return self.runtime.vision.analyze(image.convert("RGB"))

    async def process(self, frame):
        started = perf_counter()
        if (
            utcnow() - as_utc(frame.captured_at)
        ).total_seconds() > self.runtime.settings.max_frame_age_seconds:
            await self.fail(
                frame, "stale_frame", "Image trop ancienne pour produire une alerte actuelle."
            )
            return
        try:
            result = await asyncio.to_thread(self._analyze, frame.image)
            if not all(math.isfinite(value) and value >= 0 for value in result.timing_ms.values()):
                raise ValueError("Invalid vision timing")
            if (
                utcnow() - as_utc(frame.captured_at)
            ).total_seconds() > self.runtime.settings.max_frame_age_seconds:
                await self.fail(
                    frame, "stale_frame", "Résultat trop ancien pour produire une alerte actuelle."
                )
                return
            depth_masks = result.depth_masks
            result = await asyncio.to_thread(
                self.perception.enrich,
                replace(result, depth_masks=None),
                frame.session_id,
                frame.frame_id,
                frame.captured_at,
                (frame.width, frame.height),
            )
            detections = []
            for item in result.detections:
                if not math.isfinite(item.confidence) or not 0 <= item.confidence <= 1:
                    raise ValueError("Invalid detection confidence")
                if item.distance_m is not None and (
                    not math.isfinite(item.distance_m) or item.distance_m <= 0
                ):
                    raise ValueError("Invalid metric distance")
                if (
                    len(item.bbox) != 4
                    or not all(math.isfinite(v) and 0 <= v <= 1 for v in item.bbox)
                    or item.bbox[0] >= item.bbox[2]
                    or item.bbox[1] >= item.bbox[3]
                ):
                    raise ValueError("Invalid detection bounding box")
                detections.append(
                    Detection(
                        frame_id=frame.id,
                        label=item.label,
                        confidence=item.confidence,
                        bbox=list(item.bbox),
                        distance_m=item.distance_m,
                        distance_source=item.distance_source,
                        proximity=self.proximity.classify(item.distance_m, item.confidence),
                        observation_type=item.observation_type,
                        depth_valid_fraction=item.depth_valid_fraction,
                        perception=item.perception,
                    )
                )
            async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
                row = await FrameRepository(db).get_internal(frame.id)
                if (
                    utcnow() - as_utc(row.captured_at)
                ).total_seconds() > self.runtime.settings.max_frame_age_seconds:
                    await self._mark_failed(
                        db,
                        row,
                        "stale_frame",
                        "Résultat trop ancien pour produire une alerte actuelle.",
                    )
                    return
                db.add_all(detections)
                row.status = "completed"
                row.provider = result.provider
                row.simulated = result.simulated
                row.description = result.description
                row.processed_at = utcnow()
                row.latency_ms = (perf_counter() - started) * 1000
                row.timing_ms = result.timing_ms
                row.vision_metadata = {
                    **result.metadata,
                    "near_threshold_m": self.runtime.settings.near_threshold_m,
                    "far_threshold_m": self.runtime.settings.far_threshold_m,
                    "min_confidence": self.runtime.settings.min_confidence,
                }
                serialized = [detection_read(d).model_dump(mode="json") for d in detections]
                scheduled = (
                    self.decoupled and depth_masks is not None and self.runtime.depth_worker.due
                )
                if scheduled:
                    db.add(DepthJob(frame_id=row.id, masks=depth_masks, observations=serialized))
                row.vision_metadata = {
                    **row.vision_metadata,
                    "depth_sample_status": "scheduled"
                    if scheduled
                    else "not_sampled"
                    if self.decoupled
                    else "synchronous",
                }
                events = EventRepository(db)
                common = {
                    "frame_id": str(row.frame_id),
                    "provider": result.provider,
                    "simulated": result.simulated,
                    "captured_at": as_utc(row.captured_at).isoformat(),
                }
                await events.add(
                    row.session_id,
                    "frame.analyzed",
                    {
                        **common,
                        "text": result.description,
                        "detections": serialized,
                        "latency_ms": row.latency_ms,
                        "timing_ms": row.timing_ms,
                        "metadata": row.vision_metadata,
                    },
                    frame_id=row.id,
                )
                close = [d for d in serialized if d["proximity"] == "near"]
                if close:
                    nearest = min(d["distance_m"] for d in close)
                    await events.add(
                        row.session_id,
                        "alert.proximity",
                        {
                            **common,
                            "text": self.proximity.warning_text(close, simulated=result.simulated),
                            "severity": "warning",
                            "threshold_m": self.runtime.settings.near_threshold_m,
                            "nearest_distance_m": nearest,
                            "detections": close,
                            **self.proximity.audio_fields(row.captured_at, close),
                        },
                        frame_id=row.id,
                    )
                possible = [
                    d
                    for d in serialized
                    if d["perception"].get("risk") == "possible"
                    and d["perception"].get("alert_emitted", True)
                ]
                if possible:
                    await events.add(
                        row.session_id,
                        "alert.possible_proximity",
                        {
                            **common,
                            "text": self.proximity.possible_warning_text(possible),
                            "severity": "warning",
                            "ruleset": RULES["version"],
                            "detections": possible,
                            **self.proximity.audio_fields(row.captured_at, possible),
                        },
                        frame_id=row.id,
                    )
            if scheduled:
                self.runtime.depth_worker.admitted()
            logger.info(
                "Frame %s completed with %s detections in %.1fms",
                frame.frame_id,
                len(detections),
                (perf_counter() - started) * 1000,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Analysis failed for frame %s", frame.frame_id)
            await self.fail(
                frame,
                "inference_failed",
                "Analyse indisponible. La proximité ne peut pas être évaluée.",
            )

    async def fail(self, frame, code, text):
        async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
            row = await FrameRepository(db).get_internal(frame.id)
            await self._mark_failed(db, row, code, text)

    @staticmethod
    async def _mark_failed(db, row, code, text):
        row.status = "failed"
        row.error_code = code
        row.processed_at = utcnow()
        await EventRepository(db).add(
            row.session_id,
            "analysis.failed",
            {"frame_id": str(row.frame_id), "code": code, "text": text},
            frame_id=row.id,
        )
