"""Freeze analyzed captures and their own depth; never borrow another frame's metres."""

import base64
import io
from datetime import timedelta
from uuid import uuid4

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.core.time import as_utc
from app.models import Frame
from app.models.entities import DepthJob
from app.repositories.frames import FrameRepository
from app.services.serialization import detection_read


async def freeze_scene(db, session_id, request_id, text, received_at, max_age):
    frames = list(
        await db.scalars(
            select(Frame)
            .where(
                Frame.session_id == session_id,
                Frame.status == "completed",
                Frame.captured_at >= received_at - timedelta(seconds=max_age),
                Frame.captured_at <= received_at,
                Frame.processed_at <= received_at,
            )
            .order_by(Frame.captured_at.desc(), Frame.received_at.desc())
            .limit(3)
        )
    )
    if not frames:
        return None
    context = {
        "schema_version": "looki.scene-context.v1",
        "context_id": str(uuid4()),
        "session_id": str(session_id),
        "request_id": str(request_id),
        "question": text,
        "request_received_at": received_at.isoformat(),
        "frozen_at": received_at.isoformat(),
        "selection_policy": "latest_3_analyzed_before_question",
        "expected_frame_count": 3,
        "actual_frame_count": len(frames),
        "partial_context": len(frames) < 3,
        "bbox_format": "xyxy_normalized_top_left",
        "distance_semantics": "estimated_camera_z_meters",
        "frames": [],
    }
    for frame in reversed(frames):
        observations = [
            detection_read(d).model_dump(mode="json")
            for d in await FrameRepository(db).detections(frame.id)
        ]
        depth = await db.scalar(select(DepthJob).where(DepthJob.frame_id == frame.id))
        available = (
            depth is not None
            and depth.status == "completed"
            and depth.result
            and as_utc(depth.processed_at) <= received_at
        )
        if available:
            observations = depth.result["detections"]
        synchronous_depth = depth is None and any(d["distance_m"] is not None for d in observations)
        observations = sorted(
            observations,
            key=lambda d: (
                d.get("perception", {}).get("risk") in {"possible", "metric_near"},
                d["confidence"],
            ),
            reverse=True,
        )
        objects = []
        for d in observations[:32]:
            p = d.get("perception", {})
            center = (d["bbox"][0] + d["bbox"][2]) / 2
            objects.append(
                {
                    "label": d["label"],
                    "confidence": d["confidence"],
                    "bbox": d["bbox"],
                    "track_id": p.get("track_id"),
                    "motion": p.get("motion", "unknown"),
                    "risk": p.get("risk", "not_flagged"),
                    "direction": "left"
                    if center < 1 / 3
                    else "right"
                    if center > 2 / 3
                    else "front",
                    "distance_m": d["distance_m"],
                    "distance_source": d["distance_source"]
                    if d["distance_m"] is not None
                    else None,
                    "distance_quality": p.get(
                        "distance_quality",
                        "estimated" if d["distance_m"] is not None else "unavailable",
                    ),
                }
            )
        context["frames"].append(
            {
                "frame_id": str(frame.frame_id),
                "image_ref": f"image-{len(context['frames']) + 1}",
                "captured_at": as_utc(frame.captured_at).isoformat(),
                "width": frame.width,
                "height": frame.height,
                "content_type": frame.content_type,
                "simulated": frame.simulated,
                "provider": frame.provider,
                "depth_status": "available"
                if available or synchronous_depth
                else "pending"
                if depth and depth.status == "completed"
                else depth.status
                if depth
                else "not_sampled",
                "depth_source_frame_id": str(frame.frame_id)
                if available or synchronous_depth
                else None,
                "depth_computed_at": as_utc(depth.processed_at).isoformat()
                if available
                else as_utc(frame.processed_at).isoformat()
                if synchronous_depth
                else None,
                "objects": objects,
                "objects_truncated": len(observations) > 32,
            }
        )
    return context


def image_input(data):
    with Image.open(io.BytesIO(data)) as original:
        image = original.convert("RGB")
        image.thumbnail((768, 768))
        out = io.BytesIO()
        image.save(out, format="PNG")
    return {
        "type": "input_image",
        "image_url": "data:image/png;base64," + base64.b64encode(out.getvalue()).decode(),
    }


async def snapshot_images(db, session_id, context):
    from uuid import UUID

    ids = [UUID(f["frame_id"]) for f in context["frames"]]
    rows = list(
        await db.scalars(
            select(Frame)
            .where(Frame.session_id == session_id, Frame.frame_id.in_(ids))
            .options(undefer(Frame.image))
        )
    )
    by_id = {row.frame_id: row.image for row in rows}
    return [by_id[fid] for fid in ids]
