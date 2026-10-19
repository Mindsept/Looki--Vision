from app.core.time import as_utc
from app.models import Detection, Event, Frame, Session
from app.schemas.api import DetectionRead, FrameRead, ServerEvent, SessionRead


def session_read(row: Session) -> SessionRead:
    return SessionRead(
        id=row.id,
        center_id=row.center_id,
        user_id=row.user_id,
        status=row.status,
        context=row.context,
        created_at=as_utc(row.created_at),
        closed_at=as_utc(row.closed_at) if row.closed_at else None,
    )


def detection_read(row: Detection) -> DetectionRead:
    return DetectionRead(
        label=row.label,
        confidence=row.confidence,
        bbox=row.bbox,
        distance_m=row.distance_m,
        distance_source=row.distance_source,
        proximity=row.proximity,
        observation_type=row.observation_type or "region",
        depth_valid_fraction=row.depth_valid_fraction,
        perception=row.perception or {},
    )


def frame_read(row: Frame, detections: list[Detection]) -> FrameRead:
    return FrameRead(
        frame_id=row.frame_id,
        session_id=row.session_id,
        status=row.status,
        captured_at=as_utc(row.captured_at),
        received_at=as_utc(row.received_at),
        processed_at=as_utc(row.processed_at) if row.processed_at else None,
        width=row.width,
        height=row.height,
        provider=row.provider,
        simulated=row.simulated,
        description=row.description,
        error_code=row.error_code,
        latency_ms=row.latency_ms,
        timing_ms=row.timing_ms or {},
        metadata=row.vision_metadata or {},
        detections=[detection_read(d) for d in detections],
    )


def event_read(row: Event) -> ServerEvent:
    return ServerEvent(
        id=row.id,
        session_id=row.session_id,
        type=row.type,
        timestamp=as_utc(row.timestamp),
        payload=row.payload,
    )
