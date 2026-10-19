#!/usr/bin/env python3
"""Send a real video as paced JPEGs and verify durable HTTP/WebSocket results.

Run from the repository root. Requires ffmpeg on the client, not the server.
No model outputs are synthesized and the proximity threshold is never changed.
"""

import argparse
import asyncio
import hashlib
import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx
import numpy as np
import websockets
from PIL import Image


def summary(values):
    if not values:
        return None
    return {
        "count": len(values),
        "p50": float(np.percentile(values, 50)),
        "p95": float(np.percentile(values, 95)),
        "min": min(values),
        "max": max(values),
    }


async def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {args.api_key}"}
    frames = []
    received = {}
    pending = set()
    depth_pending = set()
    errors = []
    source_hash = hashlib.sha256(args.video.read_bytes()).hexdigest()
    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, timeout=20) as client:
        config = (await client.get("/api/v1/runtime")).raise_for_status().json()
        if config["vision_provider"] not in {"yolo_objects", "yolo_seg_track"}:
            raise RuntimeError("This evaluation requires the real yolo_objects provider")
        created = (
            (await client.post("/api/v1/sessions", json={"user_id": "live-video-evaluation"}))
            .raise_for_status()
            .json()
        )
        sid = created["id"]
        url = args.base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        url += f"/api/v1/sessions/{sid}/ws?after=0"
        decoder = None
        try:
            async with websockets.connect(
                url, additional_headers=headers, max_size=8_000_000
            ) as ws:

                async def collect():
                    async for raw in ws:
                        event = json.loads(raw)
                        if event["id"] is not None:
                            received[event["id"]] = event
                            if (
                                event["type"] == "frame.analyzed"
                                and event["payload"].get("metadata", {}).get("depth_sample_status")
                                == "scheduled"
                            ):
                                depth_pending.add(event["payload"]["frame_id"])
                            if event["type"] in {"frame.depth_analyzed", "depth.failed"}:
                                depth_pending.discard(event["payload"]["frame_id"])
                            if event["type"] in {"frame.analyzed", "analysis.failed"}:
                                pending.discard(event["payload"]["frame_id"])

                collector = asyncio.create_task(collect())
                width, height = 960, 540
                decoder = await asyncio.create_subprocess_exec(
                    *[
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-threads",
                        "1",
                        "-i",
                        str(args.video),
                        "-an",
                        "-vf",
                        f"fps={args.fps},scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
                        "-pix_fmt",
                        "rgb24",
                        "-f",
                        "rawvideo",
                        "pipe:1",
                    ],
                    stdout=asyncio.subprocess.PIPE,
                )
                started = perf_counter()
                index, skipped = 0, 0
                print(f"Session {sid}: genuine video input at {args.fps} fps", flush=True)
                while args.seconds is None or index / args.fps < args.seconds:
                    try:
                        pixels = await decoder.stdout.readexactly(width * height * 3)
                    except asyncio.IncompleteReadError as exc:
                        if exc.partial:
                            raise RuntimeError("Truncated decoded frame") from exc
                        break
                    if not pixels:
                        break
                    if len(pixels) != width * height * 3:
                        raise RuntimeError("Truncated decoded frame")
                    deadline = started + index / args.fps
                    await asyncio.sleep(max(0, deadline - perf_counter()))
                    media_time = index / args.fps
                    index += 1
                    if perf_counter() - deadline > 1 / args.fps:
                        skipped += 1
                        continue
                    image = Image.frombytes("RGB", (width, height), pixels)
                    jpeg = io.BytesIO()
                    image.save(jpeg, format="JPEG", quality=82)
                    fid, captured = str(uuid4()), datetime.now(UTC).isoformat()
                    pending.add(fid)
                    response = await client.post(
                        f"/api/v1/sessions/{sid}/frames",
                        data={"frame_id": fid, "timestamp": captured, "latest_only": "true"},
                        files={"image": ("frame.jpg", jpeg.getvalue(), "image/jpeg")},
                    )
                    frames.append(
                        {
                            "frame_id": fid,
                            "media_time_s": media_time,
                            "captured_at": captured,
                            "http_status": response.status_code,
                            "jpeg_bytes": len(jpeg.getvalue()),
                        }
                    )
                    if response.status_code != 202:
                        pending.discard(fid)
                        errors.append(
                            {
                                "frame_id": fid,
                                "http_status": response.status_code,
                                "body": response.text,
                            }
                        )
                sending_seconds = perf_counter() - started
                async with asyncio.timeout(args.timeout):
                    while pending:
                        if collector.done():
                            collector.result()
                            raise RuntimeError("WebSocket closed before all frames completed")
                        await asyncio.sleep(0.05)
                processing_seconds = perf_counter() - started
                async with asyncio.timeout(args.timeout):
                    while depth_pending:
                        if collector.done():
                            collector.result()
                            raise RuntimeError("WebSocket closed before depth completed")
                        await asyncio.sleep(0.05)
                await client.post(f"/api/v1/sessions/{sid}/close")
                persisted, cursor = [], 0
                while True:
                    page = (
                        (
                            await client.get(
                                f"/api/v1/sessions/{sid}/events",
                                params={"after": cursor, "limit": 500},
                            )
                        )
                        .raise_for_status()
                        .json()
                    )
                    if not page:
                        break
                    persisted.extend(page)
                    cursor = page[-1]["id"]
                async with asyncio.timeout(10):
                    while any(e["id"] not in received for e in persisted):
                        if collector.done():
                            collector.result()
                            raise RuntimeError("Missing durable WebSocket events")
                        await asyncio.sleep(0.05)
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)
                if any(received[e["id"]] != e for e in persisted):
                    errors.append("HTTP and WebSocket payloads differ")
                analyzed = [e for e in persisted if e["type"] == "frame.analyzed"]
                depth_events = [e for e in persisted if e["type"] == "frame.depth_analyzed"]
                depth_failed = [e for e in persisted if e["type"] == "depth.failed"]
                alerts = [e for e in persisted if e["type"] == "alert.proximity"]
                possible_alerts = [e for e in persisted if e["type"] == "alert.possible_proximity"]
                expected_possible = [
                    e["payload"]["frame_id"]
                    for e in analyzed
                    if any(
                        d.get("perception", {}).get("risk") == "possible"
                        and d.get("perception", {}).get("alert_emitted", True)
                        for d in e["payload"]["detections"]
                    )
                ]
                if sorted(expected_possible) != sorted(
                    e["payload"]["frame_id"] for e in possible_alerts
                ):
                    errors.append("Missing or unexpected possible proximity alert")
                for event in possible_alerts:
                    if "nearest_distance_m" in event["payload"] or any(
                        d["proximity"] == "near" or not d["perception"].get("reason_codes")
                        for d in event["payload"]["detections"]
                    ):
                        errors.append(
                            "Possible proximity must retain uncertainty and explain evidence"
                        )
                superseded = [
                    e
                    for e in persisted
                    if e["type"] == "analysis.failed" and e["payload"]["code"] == "superseded_frame"
                ]
                failed = [
                    e
                    for e in persisted
                    if e["type"] == "analysis.failed" and e["payload"]["code"] != "superseded_frame"
                ]
                expected_alerts = [
                    e["payload"]["frame_id"]
                    for e in [*analyzed, *depth_events]
                    if any(d["proximity"] == "near" for d in e["payload"]["detections"])
                ]
                if sorted(expected_alerts) != sorted(e["payload"]["frame_id"] for e in alerts):
                    errors.append("Missing or unexpected proximity alert")
                if any(
                    e["payload"]["simulated"] is not False
                    or e["payload"]["provider"] != config["vision_provider"]
                    for e in analyzed
                ):
                    errors.append("Unexpected simulated result or provider")
                if not analyzed:
                    errors.append("No actual inference completed")
                distances = [
                    d["distance_m"]
                    for e in [*analyzed, *depth_events]
                    for d in e["payload"]["detections"]
                    if d["distance_m"] is not None
                ]
                timings = {
                    key: summary(
                        [
                            e["payload"]["timing_ms"][key]
                            for e in [*analyzed, *depth_events]
                            if key in e["payload"]["timing_ms"]
                        ]
                    )
                    for key in ("detection_total_ms", "depth_total_ms", "total_vision_ms")
                }
                if config.get("decoupled_depth"):
                    if any(
                        d["distance_m"] is not None
                        for e in analyzed
                        for d in e["payload"]["detections"]
                    ):
                        errors.append("Visual result borrowed metric depth")
                    if not depth_events:
                        errors.append("No independent depth measurement")
                    for e in depth_events:
                        response = await client.get(
                            f"/api/v1/sessions/{sid}/frames/{e['payload']['frame_id']}/depth"
                        )
                        if response.raise_for_status().json()["payload"] != e["payload"]:
                            errors.append("HTTP and WS depth payload differ")
                by_class = {}
                for label in ["person", "bicycle", "car"]:
                    observations = [
                        d
                        for e in analyzed
                        for d in e["payload"]["detections"]
                        if d["label"] == label
                    ]
                    by_class[label] = {
                        "observations": len(observations),
                        "confident_observations": sum(
                            d["confidence"] >= config.get("min_confidence", 0.5)
                            for d in observations
                        ),
                        "tracked_observations": sum(
                            bool(d.get("perception", {}).get("track_id")) for d in observations
                        ),
                        "approaching_observations": sum(
                            d.get("perception", {}).get("motion") == "approaching"
                            for d in observations
                        ),
                        "possible_alert_events": sum(
                            any(d["label"] == label for d in e["payload"]["detections"])
                            for e in possible_alerts
                        ),
                    }
                report = {
                    "session_id": sid,
                    "source_sha256": source_hash,
                    "target_fps": args.fps,
                    "runtime": config,
                    "classes": by_class,
                    "sending_seconds": sending_seconds,
                    "processing_seconds": processing_seconds,
                    "sent": len(frames),
                    "accepted": sum(f["http_status"] == 202 for f in frames),
                    "client_skipped": skipped,
                    "analyzed": len(analyzed),
                    "superseded": len(superseded),
                    "failed": len(failed),
                    "alerts": len(alerts),
                    "depth_samples": len(depth_events),
                    "depth_failures": len(depth_failed),
                    "approaching_observations": sum(
                        d.get("perception", {}).get("motion") == "approaching"
                        for e in analyzed
                        for d in e["payload"]["detections"]
                    ),
                    "held_observations": sum(
                        "recent_proximity_hold" in d.get("perception", {}).get("reason_codes", [])
                        for e in analyzed
                        for d in e["payload"]["detections"]
                    ),
                    "possible_alerts": len(possible_alerts),
                    "tracked_observations": sum(
                        bool(d.get("perception", {}).get("track_id"))
                        for e in analyzed
                        for d in e["payload"]["detections"]
                    ),
                    "mask_observations": sum(
                        bool(d.get("perception", {}).get("polygons"))
                        for e in analyzed
                        for d in e["payload"]["detections"]
                    ),
                    "received_fps": len(frames) / sending_seconds,
                    "processed_fps": len(analyzed) / processing_seconds,
                    "minimum_estimated_distance_m": min(distances) if distances else None,
                    "timing_ms": timings,
                    "http_ws_equal": not any(received[e["id"]] != e for e in persisted),
                    "frames": frames,
                    "events": persisted,
                    "errors": errors,
                }
                (args.output / "results.json").write_text(
                    json.dumps(report, indent=2, ensure_ascii=False) + "\n"
                )
                print(
                    json.dumps(
                        {k: v for k, v in report.items() if k not in ("frames", "events")}, indent=2
                    ),
                    flush=True,
                )
                if errors or failed:
                    raise RuntimeError("Live evaluation failed; inspect results.json")
        finally:
            try:
                if decoder:
                    if decoder.returncode is None:
                        decoder.terminate()
                    try:
                        await asyncio.wait_for(decoder.communicate(), timeout=5)
                    except TimeoutError:
                        decoder.kill()
                        await asyncio.wait_for(decoder.communicate(), timeout=5)
            finally:
                await client.post(f"/api/v1/sessions/{sid}/close")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default=os.getenv("LOOKI_API_KEY", "looki-local-dev-key"))
    parser.add_argument("--fps", type=int, choices=range(1, 31), default=15)
    parser.add_argument("--seconds", type=float)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--output", type=Path, default=Path("output/nyc-yolo-live"))
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
