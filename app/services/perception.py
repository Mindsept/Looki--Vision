"""Bounded, session-local temporal evidence; never rescale or replace raw metres."""

from collections import OrderedDict
from dataclasses import dataclass, field, replace
from math import log
from statistics import median
from time import monotonic, perf_counter
from uuid import uuid4

from app.core.time import as_utc
from app.vision.timing import elapsed_ms
from app.vision.tracking import ByteTrackAdapter

# Ratios describe apparent size, never a calibrated physical distance.
PROFILES = {
    "person": {
        "min_height": 0.55,
        "min_width": None,
        "min_area": 0.035,
        "immediate_area": 0.08,
        "static_height": 0.72,
        "static_width": None,
        "static_area": 0.08,
        "trend_size": "height",
    },
    "bicycle": {
        "min_height": 0.32,
        "min_width": 0.28,
        "min_area": 0.012,
        "immediate_area": 0.04,
        "static_height": 0.55,
        "static_width": 0.50,
        "static_area": 0.035,
        "trend_size": "extent",
    },
    "car": {
        "min_height": 0.30,
        "min_width": 0.32,
        "min_area": 0.035,
        "immediate_area": 0.10,
        "static_height": 0.50,
        "static_width": 0.55,
        "static_area": 0.12,
        "trend_size": "extent",
    },
}
RULES = {
    "version": "urban-visual-v4",
    "labels": list(PROFILES),
    "profiles": PROFILES,
    "min_observations": 2,
    "min_history_seconds": 0.25,
    "max_gap_seconds": 2.0,
    "trend_window_seconds": 1.0,
    "trend_min_seconds": 0.35,
    "trend_min_samples": 3,
    "min_size_log_rate": 0.12,
    "min_area_log_rate": 0.24,
    "hold_seconds": 0.8,
    "repeat_alert_seconds": 2.0,
}


def size_matches(height, width, profile, prefix):
    return height >= profile[f"{prefix}_height"] or (
        profile[f"{prefix}_width"] is not None and width >= profile[f"{prefix}_width"]
    )


def visual_trend(samples, size_key="height"):
    """Median pair slopes in log size per second; no conversion to metres."""
    if (
        len(samples) < RULES["trend_min_samples"]
        or samples[-1]["timestamp"] - samples[0]["timestamp"] < RULES["trend_min_seconds"]
    ):
        return "unknown", None, None
    slopes = {size_key: [], "area": []}
    for a in samples:
        for b in samples:
            dt = b["timestamp"] - a["timestamp"]
            if dt < 0.15:
                continue
            for key in slopes:
                if a[key] > 0 and b[key] > 0:
                    slopes[key].append(log(b[key] / a[key]) / dt)
    if not all(slopes.values()):
        return "unknown", None, None
    h, a = (median(slopes[k]) for k in (size_key, "area"))
    partial = any(s["partial"] for s in samples)
    if h >= RULES["min_size_log_rate"] and a >= RULES["min_area_log_rate"]:
        return "approaching", h, a
    if h <= -RULES["min_size_log_rate"] and a <= -RULES["min_area_log_rate"]:
        return ("unknown" if partial else "receding"), h, a
    return "stable", h, a


@dataclass
class StreamState:
    tracker: object
    dimensions: tuple
    epoch: str = field(default_factory=lambda: uuid4().hex)
    captured: float = 0
    touched: float = field(default_factory=monotonic)
    history: dict = field(default_factory=dict)
    last_frame: str | None = None
    last_result: object = None


class PerceptionService:
    def __init__(self, settings, tracker_factory=ByteTrackAdapter):
        self.settings = settings
        self.tracker_factory = tracker_factory
        self.streams = OrderedDict()

    def enrich(self, result, session_id, frame_id, captured_at, dimensions):
        if result.provider != "yolo_seg_track" or result.simulated:
            return result
        started = perf_counter()
        now = monotonic()
        for key in list(self.streams):
            if now - self.streams[key].touched > 60:
                del self.streams[key]
        key, fid = str(session_id), str(frame_id)
        timestamp = as_utc(captured_at).timestamp()
        state = self.streams.get(key)
        if state and state.last_frame == fid:
            return state.last_result
        reset = (
            state is None
            or state.dimensions != dimensions
            or not 0 < timestamp - state.captured <= RULES["max_gap_seconds"]
        )
        if reset:
            state = StreamState(self.tracker_factory(), dimensions)
            self.streams[key] = state
        self.streams.move_to_end(key)
        while len(self.streams) > 64:
            self.streams.popitem(last=False)
        assignments = state.tracker.update(result.detections)
        enriched = []
        for i, item in enumerate(result.detections):
            data = dict(item.perception)
            height = item.bbox[3] - item.bbox[1]
            width = item.bbox[2] - item.bbox[0]
            profile = PROFILES.get(item.label)
            area = data.get("mask_area_ratio", 0)
            tid = assignments.get(i)
            previous = state.history.get(tid) if tid else None
            if previous and timestamp - previous["timestamp"] > RULES["max_gap_seconds"]:
                previous = None
            count = previous["count"] + 1 if previous else 1
            first = previous["first"] if previous else timestamp
            dt = timestamp - previous["timestamp"] if previous else 0
            height_growth = (
                height / previous["height"] if previous and previous["height"] > 0 else None
            )
            area_growth = area / previous["area"] if previous and previous["area"] > 0 else None
            stable = (
                tid is not None
                and count >= RULES["min_observations"]
                and timestamp - first >= RULES["min_history_seconds"]
            )
            prominent = (
                profile is not None
                and size_matches(height, width, profile, "min")
                and area >= profile["min_area"]
            )
            edge = bool(set(data.get("mask_edges", [])) & {"left", "right", "bottom"})
            occupancy = area / max(height * width, 1e-6)
            partial = bool(data.get("mask_edges")) or bool(
                previous and occupancy < previous["occupancy"] * 0.6
            )
            samples = (
                [
                    v
                    for v in previous.get("samples", [])
                    if timestamp - v["timestamp"] <= RULES["trend_window_seconds"]
                ]
                if previous
                else []
            )
            samples.append(
                {
                    "timestamp": timestamp,
                    "height": height,
                    "extent": (height * width) ** 0.5,
                    "area": area,
                    "partial": partial,
                }
            )
            samples = samples[-24:]
            size_key = profile["trend_size"] if profile else "height"
            motion, size_rate, area_rate = visual_trend(samples, size_key)
            growth = motion == "approaching"
            near = (
                item.distance_m is not None
                and item.distance_m <= self.settings.near_threshold_m
                and item.confidence >= self.settings.min_confidence
            )
            immediate = prominent and edge and area >= profile["immediate_area"]
            large_static = (
                profile is not None
                and stable
                and size_matches(height, width, profile, "static")
                and area >= profile["static_area"]
            )
            confident = item.confidence >= self.settings.min_confidence
            eligible = profile is not None and confident
            visual_trigger = eligible and (
                immediate or large_static or (stable and prominent and (edge or growth))
            )
            until = (
                timestamp + RULES["hold_seconds"]
                if visual_trigger or near
                else previous.get("until", 0)
                if previous
                else 0
            )
            # A matched weak observation may preserve an existing warning briefly,
            # but cannot create or extend the hold window on its own.
            held = (
                profile is not None
                and previous is not None
                and not visual_trigger
                and until > timestamp
            )
            possible = not near and (visual_trigger or held)
            last_alert = previous.get("last_alert", -1e20) if previous else -1e20
            emitted = possible and (
                not previous
                or previous.get("risk") != "possible"
                or timestamp - last_alert >= RULES["repeat_alert_seconds"]
            )
            if emitted:
                last_alert = timestamp
            risk = (
                "metric_near"
                if near
                else "possible"
                if possible
                else "approaching"
                if growth
                else "not_flagged"
            )
            reasons = []
            if possible:
                if held:
                    reasons.append("recent_proximity_hold")
                else:
                    reasons.append("large_object_mask")
                if stable:
                    reasons.append("persistent_track")
                if immediate:
                    reasons.append("strong_mask_at_edge")
                if large_static:
                    reasons.append("large_persistent_object")
                if edge:
                    reasons.append("mask_touches_image_edge")
                if growth:
                    reasons.append("growing_object_mask")
                if partial:
                    reasons.append("partial_visibility")
                reasons.append(
                    "depth_visual_disagreement"
                    if item.distance_m is not None
                    else "depth_unavailable"
                )
            data.update(
                {
                    "visual_profile": item.label if profile else None,
                    "detection_status": "tracked"
                    if tid
                    else "confident"
                    if confident
                    else "tentative",
                    "track_id": f"{state.epoch}:{tid}" if tid else None,
                    "track_observations": count if tid else 0,
                    "risk": risk,
                    "motion": motion,
                    "alert_emitted": emitted,
                    "distance_quality": "unavailable"
                    if item.distance_m is None
                    else "uncertain"
                    if possible
                    else "estimated",
                    "reason_codes": reasons,
                    "evidence": {
                        "bbox_height_ratio": height,
                        "bbox_width_ratio": width,
                        "size_measure": size_key,
                        "size_log_rate_per_second": size_rate,
                        "track_age_seconds": timestamp - first,
                        "interval_seconds": dt,
                        "height_growth": height_growth,
                        "area_growth": area_growth,
                        "height_log_rate_per_second": size_rate if size_key == "height" else None,
                        "area_log_rate_per_second": area_rate,
                        "trend_samples": len(samples),
                        "partial_visibility": partial,
                        "hold_remaining_seconds": max(0, until - timestamp),
                    },
                }
            )
            enriched.append(replace(item, perception=data))
            if tid:
                state.history[tid] = {
                    "timestamp": timestamp,
                    "first": first,
                    "count": count,
                    "height": height,
                    "area": area,
                    "occupancy": occupancy,
                    "samples": samples,
                    "until": until,
                    "last_alert": last_alert,
                    "risk": risk,
                }
        state.history = {k: v for k, v in state.history.items() if timestamp - v["timestamp"] <= 2}
        tracking_ms = elapsed_ms(started)
        output = replace(
            result,
            detections=enriched,
            timing_ms={
                **result.timing_ms,
                "tracking_risk_ms": tracking_ms,
                "total_vision_ms": result.timing_ms.get("total_vision_ms", 0) + tracking_ms,
            },
            metadata={
                **result.metadata,
                "tracker": "ByteTrack/class-separated",
                "tracking_cadence": "analyzed_frames_only",
                "tracking_epoch": state.epoch,
                "tracking_reset": reset,
                "proximity_rules": RULES,
            },
        )
        state.captured, state.touched = timestamp, now
        state.last_frame, state.last_result = fid, output
        return output
