"""One durable, rate-limited depth job; visual inference never waits for it."""

import asyncio
import io
import logging
import math
from copy import deepcopy
from time import monotonic, perf_counter

from PIL import Image

from app.core.time import as_utc, utcnow
from app.models.entities import DepthJob, Frame
from app.repositories.depth import DepthRepository
from app.repositories.events import EventRepository
from app.repositories.frames import FrameRepository
from app.services.proximity import ProximityService
from app.vision.masks import unpack_masks
from app.vision.segmentation import mask_depth

logger = logging.getLogger(__name__)


class DepthWorker:
    def __init__(self, runtime):
        self.runtime = runtime
        self.busy = False
        self.next_due = 0.0
        self.proximity = ProximityService(runtime.settings)

    @property
    def due(self):
        return not self.busy and monotonic() >= self.next_due

    async def recover(self):
        async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
            self.busy = bool(await DepthRepository(db).recover())

    async def run(self):
        needs_recovery = False
        while True:
            try:
                if needs_recovery:
                    await self.recover()
                    needs_recovery = False
                async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
                    job = await DepthRepository(db).claim_next()
                if job is None:
                    await asyncio.sleep(0.03)
                    continue
                self.busy = True
                await self.process(job)
                self.busy = False
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Depth worker iteration failed")
                await asyncio.sleep(1)
                needs_recovery = True

    def predict(self, image_bytes, packed_masks, observations):
        started = perf_counter()
        with Image.open(io.BytesIO(image_bytes)) as image:
            depth = self.runtime.vision.depth.predict_depth(image.convert("RGB"))
        masks = unpack_masks(packed_masks)
        if len(masks) != len(observations):
            raise ValueError("Depth masks and source detections are not aligned")
        detections = deepcopy(observations)
        for mask, detection in zip(masks, detections, strict=True):
            distance, fraction = mask_depth(
                mask, depth.depth_m, self.runtime.vision.depth.max_depth_m
            )
            detection.update(
                distance_m=distance,
                depth_valid_fraction=fraction,
                proximity=self.proximity.classify(distance, detection["confidence"]),
            )
            data = detection["perception"]
            data["alert_emitted"] = False
            data["distance_quality"] = (
                "unavailable"
                if distance is None
                else "uncertain"
                if data.get("risk") == "possible"
                else "estimated"
            )
            if detection["proximity"] == "near":
                data["risk"] = "metric_near"
                data["distance_quality"] = "estimated"
            elif data.get("risk") == "possible" and distance is not None:
                data["reason_codes"] = [
                    r for r in data.get("reason_codes", []) if r != "depth_unavailable"
                ] + ["depth_visual_disagreement"]
        timing = {**depth.timing_ms, "depth_job_total_ms": (perf_counter() - started) * 1000}
        if any(not math.isfinite(v) or v < 0 for v in timing.values()):
            raise ValueError("Invalid depth timings")
        return detections, timing, depth.metadata

    async def process(self, job):
        async with self.runtime.db.sessions() as db:
            frame = await db.get(Frame, job.frame_id)
            image = await FrameRepository(db).image_bytes(frame.session_id, frame.frame_id)
        common = {
            "frame_id": str(frame.frame_id),
            "captured_at": as_utc(frame.captured_at).isoformat(),
            "provider": "yolo_seg_track",
            "simulated": False,
        }
        error = None
        try:
            if (
                utcnow() - as_utc(frame.captured_at)
            ).total_seconds() > self.runtime.settings.depth_max_age_seconds:
                error = "stale_depth"
            else:
                detections, timing, metadata = await asyncio.to_thread(
                    self.predict, image, job.masks, job.observations
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Depth unavailable for %s", frame.frame_id)
            error = "depth_inference_failed"
        async with self.runtime.write_lock, self.runtime.db.sessions() as db, db.begin():
            row = await db.get(DepthJob, job.id)
            if row.status in {"completed", "failed"}:
                return
            age = (utcnow() - as_utc(frame.captured_at)).total_seconds()
            if age > self.runtime.settings.depth_max_age_seconds:
                error = "stale_depth"
            events = EventRepository(db)
            row.processed_at = utcnow()
            if error:
                row.status, row.error_code = "failed", error
                await events.add(
                    frame.session_id,
                    "depth.failed",
                    {
                        **common,
                        "code": error,
                        "text": "Profondeur indisponible ; le suivi visuel continue.",
                    },
                    frame_id=frame.id,
                )
                return
            row.status = "completed"
            row.result = {
                **common,
                "text": "Profondeur de cette capture uniquement.",
                "detections": detections,
                "timing_ms": timing,
                "metadata": {
                    **metadata,
                    "depth_mode": "independent_sample",
                    "source_frame_id": str(frame.frame_id),
                    "depth_model_id": self.runtime.vision.depth_model_id,
                    "result_age_seconds": age,
                },
            }
            await events.add(
                frame.session_id, "frame.depth_analyzed", row.result, frame_id=frame.id
            )
            near = [d for d in detections if d["proximity"] == "near"]
            if near:
                await events.add(
                    frame.session_id,
                    "alert.proximity",
                    {
                        **common,
                        "text": self.proximity.warning_text(near, simulated=False),
                        "severity": "warning",
                        "threshold_m": self.runtime.settings.near_threshold_m,
                        "nearest_distance_m": min(d["distance_m"] for d in near),
                        "detections": near,
                    },
                    frame_id=frame.id,
                )

    def admitted(self):
        self.busy = True
        self.next_due = monotonic() + self.runtime.settings.depth_interval_seconds
