#!/usr/bin/env python3
"""Évaluer de vraies photos via HTTP + un WebSocket et produire JSON + HTML autonomes."""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import html
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect

BACKEND_DIR = Path(__file__).resolve().parents[1]
EXPECTED_PROVIDER = "depth_anything_metric"
URBAN_PROVIDER = "urban_objects"
URBAN_TIMING_KEYS = (
    "depth_preprocess_ms",
    "depth_inference_ms",
    "depth_postprocess_ms",
    "detection_preprocess_ms",
    "detection_inference_ms",
    "detection_postprocess_ms",
    "fusion_ms",
    "total_vision_ms",
)
TIMING_LABELS = {
    "depth_preprocess_ms": "Préparation profondeur",
    "depth_inference_ms": "Inférence profondeur seule",
    "depth_postprocess_ms": "Post-traitement profondeur",
    "detection_preprocess_ms": "Préparation détecteur",
    "detection_inference_ms": "Inférence détecteur seule",
    "detection_postprocess_ms": "Post-traitement détecteur",
    "fusion_ms": "Association objets et profondeur",
    "total_vision_ms": "Vision totale",
    "analysis_latency_ms": "Traitement serveur",
    "http_to_ws_analysis_ms": "Envoi HTTP → réception WS",
}
REGIONS = (
    ("obstacle_left", "Gauche", (0.05, 0.20, 0.35, 0.80)),
    ("obstacle_center", "Centre", (0.35, 0.20, 0.65, 0.80)),
    ("obstacle_right", "Droite", (0.65, 0.20, 0.95, 0.80)),
)
CONFIG_KEYS = (
    "LOOKI_VISION_PROVIDER",
    "LOOKI_DEPTH_MODEL_ID",
    "LOOKI_DEPTH_BACKEND",
    "LOOKI_VISION_DEVICE",
    "LOOKI_NEAR_THRESHOLD_M",
    "LOOKI_FAR_THRESHOLD_M",
    "LOOKI_MIN_CONFIDENCE",
    "LOOKI_MAX_FRAME_AGE_SECONDS",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def ws_url(base_url: str, session_id: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    return urlunsplit(
        (
            "wss" if parsed.scheme == "https" else "ws",
            parsed.netloc,
            f"{parsed.path}/api/v1/sessions/{session_id}/ws",
            "after=0",
            "",
        )
    )


def config_hints() -> dict:
    """Read only non-secret local hints; these do not prove remote server settings."""
    hints = {}
    env_file = BACKEND_DIR.parent / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() in CONFIG_KEYS:
                hints[key.strip()] = {"value": value.strip().strip("\"'"), "source": "local .env"}
    for key in CONFIG_KEYS:
        if key in os.environ:
            hints[key] = {"value": os.environ[key], "source": "script environment"}
    return hints


class EventLog:
    def __init__(self, started: float):
        self.started = started
        self.items: list[dict] = []
        self.changed = asyncio.Condition()
        self.stopped = False
        self.error: str | None = None

    async def collect(self, socket) -> None:
        try:
            async for raw in socket:
                event = json.loads(raw)
                async with self.changed:
                    self.items.append(
                        {
                            "received_at": utc_now(),
                            "run_elapsed_ms": (perf_counter() - self.started) * 1000,
                            "event": event,
                        }
                    )
                    self.changed.notify_all()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            async with self.changed:
                self.stopped = True
                self.changed.notify_all()

    async def wait_for(self, predicate, wait_seconds: float) -> dict:
        async with asyncio.timeout(wait_seconds), self.changed:
            while True:
                for item in self.items:
                    if predicate(item["event"]):
                        return item
                if self.stopped:
                    raise RuntimeError(
                        self.error or "WebSocket fermé avant réception de l'événement"
                    )
                await self.changed.wait()


async def read_all_events(client: httpx.AsyncClient, session_id: str) -> list[dict]:
    result = []
    cursor = 0
    while True:
        response = await client.get(
            f"/api/v1/sessions/{session_id}/events", params={"after": cursor, "limit": 500}
        )
        response.raise_for_status()
        page = response.json()
        if not page:
            return result
        if any(event["id"] <= cursor for event in page):
            raise RuntimeError("Le journal n'avance pas son curseur exclusif.")
        ids = [event["id"] for event in page]
        if ids != sorted(set(ids)):
            raise RuntimeError("Le journal HTTP contient des identifiants désordonnés ou répétés.")
        result.extend(page)
        cursor = page[-1]["id"]


def normalized_detections(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda item: (item.get("label", ""), tuple(item.get("bbox", []))))


def validate_observation(
    frame: dict,
    persisted: list[dict],
    received: list[dict],
    expected_provider: str = EXPECTED_PROVIDER,
) -> list[str]:
    """Check externally visible invariants, including absence of unwanted alerts."""
    violations = []
    frame_id = frame["frame_id"]
    expected = [
        event for event in persisted if event.get("payload", {}).get("frame_id") == frame_id
    ]
    observed = [event for event in received if event.get("payload", {}).get("frame_id") == frame_id]
    analyzed = [event for event in expected if event["type"] == "frame.analyzed"]
    alerts = [event for event in expected if event["type"] == "alert.proximity"]
    failures = [event for event in expected if event["type"] == "analysis.failed"]
    if frame["status"] != "completed":
        violations.append(
            f"Analyse non terminée avec succès : {frame.get('error_code') or frame['status']}"
        )
        if len(failures) != 1 or analyzed or alerts:
            violations.append(
                "Une analyse échouée doit produire un seul analysis.failed et aucune analyse/alerte."
            )
    else:
        if frame.get("provider") != expected_provider or frame.get("simulated") is not False:
            violations.append(
                f"Résultat attendu : provider {expected_provider} et simulated=false."
            )
        detections = frame.get("detections", [])
        if expected_provider == URBAN_PROVIDER:
            timings = frame.get("timing_ms", {})
            missing = set(URBAN_TIMING_KEYS) - set(timings)
            if missing:
                violations.append(
                    f"Chronométrages urbains manquants : {', '.join(sorted(missing))}."
                )
            if any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
                for value in timings.values()
            ):
                violations.append(
                    "Les chronométrages doivent être des millisecondes finies et positives ou nulles."
                )
            metadata = frame.get("metadata", {})
            if not metadata.get("depth_model_id") or not metadata.get("object_model_id"):
                violations.append("Identifiants des modèles profondeur et détection manquants.")
            expected_metadata = {
                "depth_percentile": 20,
                "central_bbox_ratio": 0.8,
                "confidence_semantics": "object_detection_score",
                "depth_valid_fraction_semantics": "valid_depth_pixel_fraction",
            }
            for key, expected_value in expected_metadata.items():
                if metadata.get(key) != expected_value:
                    violations.append(f"Métadonnée de fusion inattendue : {key}.")
        for item in detections:
            if expected_provider == URBAN_PROVIDER:
                if item.get("observation_type") != "object":
                    violations.append(
                        "Le provider urbain doit exposer des objets, pas des régions fixes."
                    )
                coverage = item.get("depth_valid_fraction")
                if coverage is not None and (
                    not isinstance(coverage, (int, float))
                    or not math.isfinite(coverage)
                    or not 0 <= coverage <= 1
                ):
                    violations.append(f"Couverture profondeur invalide : {item.get('label')}")
                if item.get("distance_m") is not None and (coverage is None or coverage < 0.5):
                    violations.append(
                        f"Distance attribuée sans couverture profondeur suffisante : {item.get('label')}"
                    )
            distance = item.get("distance_m")
            if distance is not None and (not math.isfinite(distance) or distance <= 0):
                violations.append(f"Distance non métrique exploitable : {item.get('label')}")
            if item.get("distance_source") != "metric_model":
                violations.append(f"Source de distance inattendue : {item.get('label')}")
        if len(analyzed) != 1 or failures:
            violations.append(
                "Un résultat completed doit avoir exactement un frame.analyzed et aucun échec."
            )
        for event in analyzed:
            payload = event["payload"]
            if (
                payload.get("provider") != expected_provider
                or payload.get("simulated") is not False
            ):
                violations.append("frame.analyzed indique un provider simulé ou inattendu.")
            if expected_provider == URBAN_PROVIDER:
                for field in ("timing_ms", "metadata"):
                    if payload.get(field) != frame.get(field):
                        violations.append(f"Le champ {field} diffère entre HTTP et frame.analyzed.")
            if normalized_detections(payload.get("detections", [])) != normalized_detections(
                detections
            ):
                violations.append("Les détections HTTP et WebSocket persistées diffèrent.")
        close = [item for item in detections if item.get("proximity") == "near"]
        if len(alerts) != int(bool(close)):
            violations.append(
                f"{len(alerts)} alerte(s) persistée(s), attendu {int(bool(close))} pour {len(close)} observation(s) near."
            )
        for event in alerts:
            payload = event["payload"]
            if (
                payload.get("provider") != expected_provider
                or payload.get("simulated") is not False
            ):
                violations.append("L'alerte indique un provider simulé ou inattendu.")
            if not payload.get("text"):
                violations.append("L'alerte n'a pas de texte à vocaliser.")
            if normalized_detections(payload.get("detections", [])) != normalized_detections(close):
                violations.append("L'alerte ne regroupe pas exactement les observations near.")
            if close and not math.isclose(
                payload.get("nearest_distance_m", -1),
                min(item["distance_m"] for item in close),
                abs_tol=1e-6,
            ):
                violations.append(
                    "La distance de l'alerte ne correspond pas à la observation near la plus proche."
                )
    for event in expected:
        if not any(message == event for message in observed):
            violations.append(
                f"Événement durable {event['id']} ({event['type']}) non reçu à l'identique en WS."
            )
    if any(event not in expected for event in observed):
        violations.append("Un événement WS lié à l'image est absent du journal exhaustif HTTP.")
    return violations


def load_image(images_dir: Path, filename: str) -> tuple[Path, bytes]:
    image_root = images_dir.resolve()
    path = (image_root / filename).resolve()
    if not path.is_relative_to(image_root):
        raise ValueError(f"Chemin image hors dossier autorisé : {filename}")
    return path, path.read_bytes()


async def evaluate_image(
    client,
    socket_log: EventLog,
    session_id: str,
    entry: dict,
    images_dir: Path,
    wait_seconds: float,
    expected_provider: str = EXPECTED_PROVIDER,
    sample_index: int = 1,
    repeat_index: int = 1,
    image_index: int = 1,
) -> dict:
    filename = entry["filename"]
    path, image = await asyncio.to_thread(load_image, images_dir, filename)
    record = {
        "filename": filename,
        "sample_index": sample_index,
        "repeat_index": repeat_index,
        "image_index": image_index,
        "is_first_sample": sample_index == 1,
        "is_first_sample_for_image": repeat_index == 1,
        "source": entry,
        "image_path": str(path),
        "image_sha256": hashlib.sha256(image).hexdigest(),
        "frame_id": str(uuid4()),
        "session_id": session_id,
        "upload_timestamp": utc_now(),
        "violations": [],
        "http_status_history": [],
        "frame": None,
        "persisted_events": [],
        "received_events": [],
        "status": "running",
    }
    if entry.get("sha256") and entry["sha256"] != record["image_sha256"]:
        record["violations"].append("SHA-256 de la photo différent de celui du manifeste.")
        record["status"] = "failed"
        return record
    started = perf_counter()
    record["upload_started_run_elapsed_ms"] = (started - socket_log.started) * 1000
    try:
        response = await client.post(
            f"/api/v1/sessions/{session_id}/frames",
            data={"frame_id": record["frame_id"], "timestamp": record["upload_timestamp"]},
            files={"image": (filename, image, "image/jpeg")},
        )
        record["upload_http_status"] = response.status_code
        record["upload_response_ms"] = (perf_counter() - started) * 1000
        response.raise_for_status()
        if response.status_code != 202:
            record["violations"].append(f"Nouvel upload attendu 202, reçu {response.status_code}.")
        record["upload_response"] = response.json()
        async with asyncio.timeout(wait_seconds):
            while True:
                response = await client.get(
                    f"/api/v1/sessions/{session_id}/frames/{record['frame_id']}"
                )
                response.raise_for_status()
                frame = response.json()
                if (
                    not record["http_status_history"]
                    or record["http_status_history"][-1]["status"] != frame["status"]
                ):
                    record["http_status_history"].append(
                        {
                            "status": frame["status"],
                            "at": utc_now(),
                            "elapsed_ms": (perf_counter() - started) * 1000,
                        }
                    )
                record["frame"] = frame
                if frame["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.1)
        record["http_terminal_elapsed_ms"] = (perf_counter() - started) * 1000
        persisted = await read_all_events(client, session_id)
        record["persisted_events"] = [
            event
            for event in persisted
            if event.get("payload", {}).get("frame_id") == record["frame_id"]
        ]
        # Completed frame and its events are committed in the same transaction.
        # Reading the exhaustive journal now also proves absence of an alert.
        for event in record["persisted_events"]:
            await socket_log.wait_for(
                lambda received, event_id=event["id"]: received.get("id") == event_id, wait_seconds
            )
        record["received_events"] = [
            item
            for item in socket_log.items
            if item["event"].get("payload", {}).get("frame_id") == record["frame_id"]
        ]
        analysis = next(
            (
                item
                for item in record["received_events"]
                if item["event"]["type"] == "frame.analyzed"
            ),
            None,
        )
        record["analysis_latency_ms"] = record["frame"].get("latency_ms")
        record["timing_ms"] = record["frame"].get("timing_ms", {})
        record["metadata"] = record["frame"].get("metadata", {})
        record["http_to_ws_analysis_ms"] = (
            analysis["run_elapsed_ms"] - record["upload_started_run_elapsed_ms"]
            if analysis
            else None
        )
        record["violations"].extend(
            validate_observation(
                record["frame"],
                persisted,
                [item["event"] for item in record["received_events"]],
                expected_provider,
            )
        )
    except Exception as exc:
        record["violations"].append(f"{type(exc).__name__}: {exc}")
        if isinstance(exc, httpx.HTTPStatusError):
            record["error_response"] = exc.response.text
    record["status"] = "passed" if not record["violations"] else "failed"
    record["finished_at"] = utc_now()
    return record


def safe_link(value: str | None, label: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"}:
        return html.escape(value)
    return f'<a href="{html.escape(value, quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(label)}</a>'


def number(value, suffix="") -> str:
    return f"{value:.2f}{suffix}" if isinstance(value, (int, float)) else "—"


REPORT_CSS = ":root{--ink:#172d32;--muted:#52676d;--paper:#f3f6f3;--line:#dce6df;--near:#b7432f;--medium:#8b620b;--far:#126f73}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 system-ui,-apple-system,sans-serif}main{max-width:1340px;margin:auto;padding:44px 30px 70px}header{padding:12px 0 26px;border-bottom:1px solid var(--line)}.eyebrow{font-size:11px;font-weight:750;letter-spacing:.14em;margin:0 0 8px;color:var(--far)}h1{font-size:clamp(30px,4vw,48px);line-height:1.12;margin:8px 0 14px;letter-spacing:-.04em;max-width:850px}h2{font-size:21px;line-height:1.25;margin:0}h3{font-size:13px;margin:0 0 9px}p{margin:8px 0}.intro{max-width:900px;color:var(--muted)}.summary{display:flex;gap:40px;padding:24px 0;flex-wrap:wrap}.summary b{display:block;font-size:30px;font-weight:650}.summary span{color:var(--muted);font-size:13px}.method{background:#e3ece6;border-left:3px solid #247c67;padding:17px 22px;margin:0 0 27px;max-width:1080px}.method p{font-size:13px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px;align-items:start}.card{background:#fff;border:1px solid var(--line);border-radius:14px;overflow:hidden}.card-heading{padding:22px;display:flex;align-items:start;justify-content:space-between;gap:20px}.card-heading .eyebrow{margin-bottom:6px}.verdict{font-size:11px;white-space:nowrap;border-radius:20px;padding:5px 10px;font-weight:700}.passed{background:#e2f0e8;color:#245f45}.failed{background:#fae2da;color:#8c3022}.photo{position:relative;line-height:0;background:#122126}.photo img{display:block;width:100%;height:auto}.roi{position:absolute;border:2px solid currentColor;line-height:1.15;background:transparent;pointer-events:none}.roi span{position:absolute;left:3px;top:3px;padding:4px 7px;background:#102128e8;color:white;font-size:10px;border-radius:4px}.roi strong{font-size:13px}.near{color:var(--near)}.medium{color:var(--medium)}.far{color:var(--far)}.unknown{color:#647780}.roi.near{border-color:#ff735c}.roi.medium{border-color:#ffcd59}.roi.far{border-color:#5ce0cd}.roi.unknown{border-color:#bbcbd0;border-style:dashed}.card-body{padding:20px 22px}.metrics{display:flex;gap:28px;color:var(--muted);font-size:11px;margin-bottom:19px}.metrics b{display:block;font-size:19px;color:var(--ink)}table{width:100%;border-collapse:collapse;font-size:12px}th{text-align:left;font-weight:500;color:var(--muted);padding:0 5px 8px}td{padding:10px 5px;border-top:1px solid var(--line)}.pill{display:inline-block;background:#f2f5f4;padding:2px 7px;border-radius:5px;font-size:10px;font-weight:700}.alert-box{margin-top:18px;padding:13px 15px;background:#f3f6f5;border-radius:7px}.alert-box.has-alert{background:#fff0e9;color:#8d392a}blockquote{font-size:14px;margin:0}.no-alert,.source{font-size:11px;color:var(--muted)}.source{margin:17px 0}a{color:var(--far)}details{margin-top:10px;font-size:12px}summary{cursor:pointer;color:var(--muted)}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f6f5;padding:12px;border-radius:5px;max-height:360px;overflow:auto;font-size:11px}.errors{color:#9d3224;font-size:12px}footer{margin-top:30px;color:var(--muted);font-size:12px}@media(max-width:1000px){main{padding:24px 14px}.grid{grid-template-columns:1fr}.summary{gap:24px}.card-heading{padding:17px}.card-body{padding:17px}}@media print{body{background:white}main{padding:0}.grid{display:block}.card{break-inside:avoid;margin-bottom:22px}details{display:none}}.table-scroll{overflow-x:auto;margin-top:12px}.benchmark{padding:24px;margin:0 0 28px;background:white;border:1px solid var(--line);border-radius:14px}.benchmark h2{margin-bottom:20px}.muted{font-size:11px;color:var(--muted);margin:10px 0 16px}.samples{margin-top:20px}.samples summary{margin-bottom:12px}.metrics{flex-wrap:wrap;gap:18px}.roi span{z-index:2;white-space:nowrap}.roi{border-width:2px}.objects .roi{pointer-events:auto;outline-offset:3px}.objects .roi.compact .object-detail{opacity:0;visibility:hidden;z-index:5}.objects .roi.compact:hover .object-detail,.objects .roi.compact:focus .object-detail{opacity:1;visibility:visible}.objects .roi.compact:hover .bbox-number,.objects .roi.compact:focus .bbox-number{opacity:0}.objects .roi:hover,.objects .roi:focus{z-index:8}.objects .roi .bbox-number{padding:2px 4px;font-size:9px}.objects .roi:focus{outline:2px solid white}.roi.align-right span{left:auto;right:3px}@media(max-width:600px){main{padding:18px 10px}.card-heading,.card-body{padding:14px}.benchmark{padding:17px}.summary{gap:18px}.summary b{font-size:25px}.table-scroll table{font-size:11px}}"


def percentile(values: list[float], fraction: float) -> float:
    """Inclusive linear quantile; every sample, including the first, is retained."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("Cannot compute a percentile without samples")
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def latency_values(record: dict) -> dict:
    values = dict(record.get("timing_ms") or (record.get("frame") or {}).get("timing_ms") or {})
    values.update(
        {key: record.get(key) for key in ("analysis_latency_ms", "http_to_ws_analysis_ms")}
    )
    return {
        key: float(value)
        for key, value in values.items()
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    }


def summarize_latencies(records: list[dict]) -> dict:
    metric_values: dict[str, list[float]] = {}
    for record in records:
        for key, value in latency_values(record).items():
            metric_values.setdefault(key, []).append(value)
    return {
        key: {
            "count": len(values),
            "p50_ms": percentile(values, 0.5),
            "p95_ms": percentile(values, 0.95),
            "min_ms": min(values),
            "max_ms": max(values),
        }
        for key, values in sorted(metric_values.items())
    }


def build_benchmark(report: dict) -> dict:
    records = report["images"]
    grouped: dict[str, list[dict]] = {}
    for record in records:
        grouped.setdefault(record["filename"], []).append(record)
    return {
        "sample_count": len(records),
        "planned_samples": report.get("planned_samples", len(records)),
        "unique_images": len(grouped),
        "repeat_count": report.get("repeat_count", 1),
        "passed_samples": sum(record["status"] == "passed" for record in records),
        "failed_samples": sum(record["status"] == "failed" for record in records),
        "first_sample_retained": True,
        "quantile_method": "Linear interpolation at (n - 1) * q; all finite observed samples retained, including the first. Counts exclude only unavailable measurements, not slow samples.",
        "metrics": summarize_latencies(records),
        "by_image": {
            filename: {"sample_count": len(items), "metrics": summarize_latencies(items)}
            for filename, items in grouped.items()
        },
    }


def valid_bbox(bbox) -> bool:
    return (
        isinstance(bbox, (list, tuple))
        and len(bbox) == 4
        and all(
            isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1
            for value in bbox
        )
        and bbox[0] < bbox[2]
        and bbox[1] < bbox[3]
    )


def render_detections(frame: dict, urban: bool) -> tuple[str, str]:
    items = frame.get("detections", [])
    if urban:
        observations = [
            (f"{item.get('label', 'objet')} #{index}", item, None)
            for index, item in enumerate(items, start=1)
        ]
        headings = "<th>Objet détecté</th><th>Distance</th><th>Score détection</th><th>Pixels profondeur valides</th><th>Proximité</th>"
    else:
        by_label = {item["label"]: item for item in items}
        observations = [(name, by_label.get(label, {}), bbox) for label, name, bbox in REGIONS]
        headings = "<th>Zone</th><th>Distance estimée</th><th>Pixels valides</th><th>Proximité</th>"
    overlays, rows = [], []
    for observation_index, (name, detection, fallback_bbox) in enumerate(observations, start=1):
        state = detection.get("proximity", "unknown")
        if state not in {"near", "medium", "far", "unknown"}:
            state = "unknown"
        bbox = detection.get("bbox", fallback_bbox)
        if not valid_bbox(bbox):
            bbox = fallback_bbox
        distance = number(detection.get("distance_m"), " m")
        coverage = detection.get("depth_valid_fraction")
        if not urban and coverage is None:
            coverage = detection.get("confidence")
        coverage_text = number(coverage * 100 if coverage is not None else None, " %")
        safe_name = html.escape(name)
        if valid_bbox(bbox):
            x1, y1, x2, y2 = bbox
            compact = urban and len(observations) > 6
            compact_class = " compact" if compact else ""
            edge_class = " align-right" if x1 > 0.65 or x2 > 0.95 else ""
            number_chip = (
                f'<span class="bbox-number">#{observation_index}</span>' if compact else ""
            )
            interaction = (
                f' tabindex="0" aria-label="{html.escape(name + ": " + distance + ", " + state, quote=True)}"'
                if urban
                else ""
            )
            overlays.append(
                f'<div class="roi {state}{compact_class}{edge_class}"{interaction} style="left:{x1 * 100:.2f}%;top:{y1 * 100:.2f}%;width:{(x2 - x1) * 100:.2f}%;height:{(y2 - y1) * 100:.2f}%">{number_chip}<span class="object-detail">{safe_name}<br><strong>{distance}</strong></span></div>'
            )
        confidence = detection.get("confidence")
        score_cell = (
            f"<td>{number(confidence * 100 if confidence is not None else None, ' %')}</td>"
            if urban
            else ""
        )
        rows.append(
            f'<tr><td>{safe_name}</td><td>{distance}</td>{score_cell}<td>{coverage_text}</td><td><span class="pill {state}">{state}</span></td></tr>'
        )
    if not rows:
        rows.append(
            '<tr><td colspan="5">Aucun objet renvoyé par le détecteur pour cet échantillon.</td></tr>'
        )
    return (
        "".join(overlays),
        f'<div class="table-scroll"><table><thead><tr>{headings}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>',
    )


def render_benchmark(benchmark: dict, urban: bool) -> str:
    order = [
        "depth_inference_ms",
        "detection_inference_ms",
        "total_vision_ms",
        "analysis_latency_ms",
        "http_to_ws_analysis_ms",
    ]
    rows = []
    for key in order:
        stats = benchmark["metrics"].get(key)
        if stats is None:
            continue
        rows.append(
            f"<tr><td>{html.escape(TIMING_LABELS[key])}</td><td>{stats['count']}</td><td>{number(stats['p50_ms'])}</td><td>{number(stats['p95_ms'])}</td><td>{number(stats['min_ms'])}</td><td>{number(stats['max_ms'])}</td></tr>"
        )
    if not rows:
        return ""
    details = []
    for key, stats in benchmark["metrics"].items():
        if key in order:
            continue
        details.append(
            f"<tr><td>{html.escape(TIMING_LABELS.get(key, key))}</td><td>{stats['count']}</td><td>{number(stats['p50_ms'])}</td><td>{number(stats['p95_ms'])}</td><td>{number(stats['min_ms'])}</td><td>{number(stats['max_ms'])}</td></tr>"
        )
    table_header = "<thead><tr><th>Étape</th><th>n</th><th>p50 (ms)</th><th>p95 (ms)</th><th>Min (ms)</th><th>Max (ms)</th></tr></thead>"
    extra = (
        f'<details><summary>Préparation, post-traitement et fusion</summary><div class="table-scroll"><table>{table_header}<tbody>{"".join(details)}</tbody></table></div></details>'
        if details
        else ""
    )
    return f'<section class="benchmark"><p class="eyebrow">LATENCES MESURÉES · {benchmark["sample_count"]} ÉCHANTILLONS</p><h2>{"Profondeur et détection chronométrées séparément" if urban else "Distribution des temps mesurés"}</h2><div class="table-scroll"><table>{table_header}<tbody>{"".join(rows)}</tbody></table></div>{extra}<p class="muted">Premier échantillon conservé. p50/p95 : interpolation linéaire sur tous les temps disponibles, sans retrait des essais lents. Le nombre n est indiqué pour chaque métrique ; les échecs restent dans les preuves. Modèles déjà chargés : ces durées ne mesurent pas leur chargement initial et ne garantissent pas une cadence caméra.</p></section>'


def render_sample_table(records: list[dict], urban: bool) -> str:
    rows = []
    for record in records:
        timings = latency_values(record)
        alert_count = sum(
            event["type"] == "alert.proximity" for event in record["persisted_events"]
        )
        number_text = str(record.get("sample_index", 1)) + (
            " · premier" if record.get("is_first_sample") else ""
        )
        stage_cells = (
            "".join(
                f"<td>{number(timings.get(key))}</td>"
                for key in ("depth_inference_ms", "detection_inference_ms", "total_vision_ms")
            )
            if urban
            else ""
        )
        rows.append(
            f"<tr><td>{number_text}</td><td>{record.get('repeat_index', 1)}</td>{stage_cells}<td>{number(record.get('http_to_ws_analysis_ms'))}</td><td>{alert_count}</td><td>{html.escape(record['status'])}</td></tr>"
        )
    stages = "<th>Depth ms</th><th>Détecteur ms</th><th>Vision ms</th>" if urban else ""
    return f'<details class="samples" open><summary>Tous les essais de cette photo ({len(records)})</summary><div class="table-scroll"><table><thead><tr><th>Échantillon</th><th>Tour</th>{stages}<th>HTTP→WS ms</th><th>Alertes</th><th>État</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></details>'


def render_html(report: dict) -> str:
    urban = report.get("expected_provider", EXPECTED_PROVIDER) == URBAN_PROVIDER
    reference_note = (
        "Le corpus possède des cartes de référence. Ce rapport vérifie le protocole et les alertes calculées ; la précision des mètres est évaluée séparément dans le rapport comparatif métrique."
        if report.get("ground_truth_available")
        else "Photos sans distances physiques de référence : il s’agit d’estimations du modèle, pas d’une validation métrologique."
    )
    model_ids = sorted(
        {
            record.get("frame", {}).get("metadata", {}).get("depth_model_id", "non renseigné")
            for record in report["images"]
            if record.get("frame")
        }
    )
    benchmark = build_benchmark(report)
    grouped: dict[str, list[dict]] = {}
    for record in report["images"]:
        grouped.setdefault(record["filename"], []).append(record)
    cards = []
    for index, records in enumerate(grouped.values(), start=1):
        record = records[0]  # Always show the first sample; never select a favorable repetition.
        frame = record.get("frame") or {}
        source = record["source"]
        image_path = Path(record["image_path"])
        photo = base64.b64encode(image_path.read_bytes()).decode() if image_path.is_file() else ""
        overlays, detection_table = render_detections(frame, urban)
        alerts = [
            event for event in record["persisted_events"] if event["type"] == "alert.proximity"
        ]
        all_alerts_received = all(
            any(item["event"] == event for item in record["received_events"]) for event in alerts
        )
        alert_title = (
            (
                "Alerte reçue et persistée"
                if all_alerts_received
                else "Alerte persistée — réception non confirmée"
            )
            if alerts
            else "Résultat sans alerte"
        )
        alert_block = (
            "".join(
                f"<blockquote>{html.escape(event['payload'].get('text', ''))}</blockquote>"
                for event in alerts
            )
            or '<p class="no-alert">Aucune alerte persistée pour ce premier échantillon de la photo.</p>'
        )
        errors = "".join(
            f"<li>Essai {item.get('sample_index', 1)} : {html.escape(error)}</li>"
            for item in records
            for error in item["violations"]
        )
        status = "passed" if all(item["status"] == "passed" for item in records) else "failed"
        author = source.get("author") or "Auteur non renseigné"
        if not isinstance(author, str):
            author = json.dumps(author, ensure_ascii=False)
        label = str(source.get("label") or source.get("description") or record["filename"])
        timings = latency_values(record)
        metric_cells = "".join(
            f"<span><b>{number(timings.get(key), ' ms')}</b>{title}</span>"
            for key, title in (
                (
                    ("depth_inference_ms", "profondeur seule"),
                    ("detection_inference_ms", "détecteur seul"),
                    ("total_vision_ms", "vision totale"),
                )
                if urban
                else (
                    ("analysis_latency_ms", "traitement serveur"),
                    ("http_to_ws_analysis_ms", "HTTP → réception WS"),
                )
            )
        )
        sample_caption = f"Annotations et distances : premier essai de cette photo, échantillon {record.get('sample_index', index)}."
        if urban and len(frame.get("detections", [])) > 6:
            sample_caption += " Toutes les boîtes sont numérotées ; classes et distances figurent dans le tableau. Survoler une boîte ou lui donner le focus pour afficher son détail."
        evidence = [
            {key: value for key, value in item.items() if key not in {"source", "image_path"}}
            for item in records
        ]
        cards.append(f'''<article class="card">
<div class="card-heading"><div><p class="eyebrow">PHOTO {index:02d} · {len(records)} ESSAI(S)</p><h2>{html.escape(label)}</h2></div><span class="verdict {status}">{"Conforme" if status == "passed" else "Échec"}</span></div>
<div class="photo {"objects" if urban else ""}"><img src="data:image/jpeg;base64,{photo}" alt="{html.escape(label, quote=True)}">{overlays}</div>
<div class="card-body"><p class="muted">{sample_caption}</p><div class="metrics">{metric_cells}</div>{detection_table}
<div class="alert-box {"has-alert" if alerts else ""}"><h3>{alert_title}</h3>{alert_block}</div>
{render_sample_table(records, urban)}{'<ul class="errors">' + errors + "</ul>" if errors else ""}
<p class="source">{safe_link(source.get("source_url"), "Source de la photo")} · {html.escape(author)} · {safe_link(source.get("license_url"), str(source.get("license") or "Licence non documentée")) or html.escape(str(source.get("license") or "Licence non documentée"))}</p>
<details><summary>Identifiants et preuves de tous les essais</summary><pre>{html.escape(json.dumps(evidence, ensure_ascii=False, indent=2))}</pre></details></div></article>''')
    alert_count = sum(
        sum(event["type"] == "alert.proximity" for event in record["persisted_events"])
        for record in report["images"]
    )
    issues = "".join(f"<li>{html.escape(issue)}</li>" for issue in report.get("errors", []))
    title = (
        "Objets urbains, distances estimées et latences."
        if urban
        else "Distances estimées et alertes sur des photos réelles."
    )
    intro = (
        "Détection d’objets et profondeur métrique sont exécutées localement et chronométrées séparément. Chaque essai passe par HTTP puis par une seule connexion WebSocket ; ses événements sont comparés au journal PostgreSQL."
        if urban
        else "Chaque photo passe par le backend HTTP, le vrai provider Depth Anything métrique et une seule connexion WebSocket. Le rapport compare les événements reçus au journal PostgreSQL."
    )
    explanation = (
        "Les cadres sont les boîtes complètes des objets renvoyés par le détecteur, avec leurs labels COCO. La distance vient du percentile 20 de profondeur brute sur les 80 % centraux de chaque boîte, avec au moins 50 % de pixels valides. Le score de détection et la couverture de profondeur sont distincts ; aucun ne représente une précision métrique."
        if urban
        else "Trois zones d’échantillonnage fixes : gauche, centre et droite. Les cadres ne sont pas des boîtes de reconnaissance d’objets. Les distances représentent le percentile 10 des pixels valides de chaque zone. La couverture n’est pas une précision métrique."
    )
    return f"""<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LOOKi — {title}</title><style>{REPORT_CSS}</style><main><header><p class="eyebrow">LOOKi / VALIDATION DES MODÈLES LOCAUX</p><h1>{title}</h1><p class="intro">{intro}</p><p class="intro">Exécution : {html.escape(report["started_at"])} · Statut : {html.escape(report["status"])}</p></header>
<div class="summary"><div><b>{len(grouped)}</b><span>photos distinctes</span></div><div><b>{benchmark["passed_samples"]}/{len(report["images"])}</b><span>essais conformes · {report.get("repeat_count", 1)} tour(s)</span></div><div><b>{alert_count}</b><span>alertes de proximité</span></div><div><b>{1 if report.get("session_id") else 0}</b><span>connexion WS bidirectionnelle</span></div></div>
<div class="method"><p><strong>Modèle de profondeur observé.</strong> {html.escape(", ".join(model_ids))}</p><p><strong>Ce que signifient les cadres.</strong> {explanation}</p><p><strong>Limite de cette évaluation.</strong> {reference_note} Aucune carte de profondeur n’est reconstruite ici. Une absence d’alerte n’établit pas l’absence d’obstacle.</p><p><strong>Temps mesurés.</strong> « Envoi HTTP → réception WS » commence juste avant l’upload et finit à la réception réelle de frame.analyzed. Ce n’est pas une mesure caméra → écouteurs. Pour UniDepthV2, « inférence profondeur » inclut le pré/post-traitement interne de son API infer ; la portée exacte est conservée dans les métadonnées. Les annotations affichent toujours le premier essai de chaque photo ; toutes les répétitions restent dans les tableaux et le JSON.</p></div>
{render_benchmark(benchmark, urban)}{'<ul class="errors">' + issues + "</ul>" if issues else ""}<div class="grid">{"".join(cards)}</div>
<footer><p>Session : {html.escape(str(report.get("session_id") or "non créée"))} · Événements, modèles et chronométrages complets dans results.json.</p><details><summary>Provider et configuration observée</summary><pre>{html.escape(json.dumps({"health": report.get("health"), "configuration_hints": report.get("configuration_hints"), "configuration_note": report.get("configuration_note")}, ensure_ascii=False, indent=2))}</pre></details><p>Rapport autonome : photos embarquées, aucun script ni ressource distante.</p></footer></main></html>"""


def save_report(report: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    report["benchmark"] = build_benchmark(report)
    (output_dir / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "report.html").write_text(render_html(report), encoding="utf-8")


async def run(args) -> dict:
    started = perf_counter()
    expected_provider = getattr(args, "expected_provider", EXPECTED_PROVIDER)
    repeat_count = getattr(args, "repeat", 1)
    report = {
        "schema_version": "1.1",
        "started_at": utc_now(),
        "finished_at": None,
        "base_url": args.base_url,
        "expected_provider": expected_provider,
        "repeat_count": repeat_count,
        "first_sample_retained": True,
        "sample_order": "Complete manifest order per round; all rounds retained, including first sample.",
        "ground_truth_available": False,
        "status": "running",
        "session_id": None,
        "configuration_hints": config_hints(),
        "configuration_note": "Les valeurs locales sont indicatives. Le provider est observé via /health/ready ; les identifiants des modèles, paramètres de fusion et seuils appliqués sont exposés dans metadata des résultats HTTP et des événements frame.analyzed.",
        "source_manifest": None,
        "images": [],
        "errors": [],
        "websocket_messages": [],
        "websocket_connection_count": 0,
    }
    socket_log = EventLog(started)
    try:
        manifest = json.loads((args.images_dir / "manifest.json").read_text())
        report["ground_truth_available"] = bool(manifest.get("ground_truth_available", False))
        entries = manifest["images"]
        if not entries:
            raise ValueError("Le manifeste ne contient aucune photo.")
        report["source_manifest"] = manifest
        report["planned_samples"] = len(entries) * repeat_count
        headers = {"Authorization": f"Bearer {args.api_key}"}
        async with httpx.AsyncClient(
            base_url=args.base_url.rstrip("/"), headers=headers, timeout=args.timeout
        ) as client:
            health = await client.get("/health/ready")
            report["health"] = {"status_code": health.status_code, "body": health.json()}
            health.raise_for_status()
            if health.json().get("vision_provider") != expected_provider:
                raise RuntimeError(
                    f"Le backend ne déclare pas le provider attendu {expected_provider}."
                )
            response = await client.post(
                "/api/v1/sessions",
                json={"center_id": "demo-centre", "user_id": "real-depth-evaluation"},
            )
            response.raise_for_status()
            session_id = report["session_id"] = response.json()["id"]
            try:
                async with connect(
                    ws_url(args.base_url, session_id),
                    additional_headers=headers,
                    open_timeout=args.timeout,
                    max_size=1024 * 1024,
                ) as socket:
                    report["websocket_connection_count"] += 1
                    collector = asyncio.create_task(socket_log.collect(socket))
                    try:
                        initial = await socket_log.wait_for(
                            lambda event: (
                                event.get("type") == "session.state" and event.get("id") is None
                            ),
                            args.timeout,
                        )
                        if initial["event"]["payload"].get("vision_provider") != expected_provider:
                            raise RuntimeError(
                                f"L'état initial WS ne déclare pas {expected_provider}."
                            )
                        await socket.send(json.dumps({"type": "ping"}))
                        await socket_log.wait_for(
                            lambda event: event.get("type") == "pong", args.timeout
                        )
                        for repeat_index in range(1, repeat_count + 1):
                            for image_index, entry in enumerate(entries, start=1):
                                sample_index = len(report["images"]) + 1
                                record = await evaluate_image(
                                    client,
                                    socket_log,
                                    session_id,
                                    entry,
                                    args.images_dir,
                                    args.timeout,
                                    expected_provider,
                                    sample_index,
                                    repeat_index,
                                    image_index,
                                )
                                report["images"].append(record)
                                report["websocket_messages"] = socket_log.items
                                save_report(report, args.output_dir)
                                distance_text = ", ".join(
                                    f"{item['label']}={item['distance_m']:.2f}m"
                                    for item in (record.get("frame") or {}).get("detections", [])
                                    if item.get("distance_m") is not None
                                )
                                print(
                                    f"Échantillon {sample_index}, tour {repeat_index}: {record['filename']}: {record['status']} — {distance_text or 'distance indisponible'}",
                                    flush=True,
                                )
                        report["session_event_history"] = await read_all_events(client, session_id)
                    finally:
                        collector.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await collector
            finally:
                closed = await client.post(f"/api/v1/sessions/{session_id}/close")
                report["session_close_http_status"] = closed.status_code
                closed.raise_for_status()
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    report["websocket_messages"] = socket_log.items
    report["finished_at"] = utc_now()
    report["total_elapsed_ms"] = (perf_counter() - started) * 1000
    report["status"] = (
        "passed"
        if report["images"]
        and not report["errors"]
        and all(item["status"] == "passed" for item in report["images"])
        else "failed"
    )
    save_report(report, args.output_dir)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", default=os.getenv("LOOKI_API_KEY", "looki-local-dev-key"))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--expected-provider",
        choices=[EXPECTED_PROVIDER, URBAN_PROVIDER],
        default=EXPECTED_PROVIDER,
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Nombre de tours complets ; premier échantillon conservé.",
    )
    parser.add_argument("--images-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout doit être strictement positif.")
    if args.repeat < 1:
        parser.error("--repeat doit être un entier supérieur ou égal à 1.")
    urban = args.expected_provider == URBAN_PROVIDER
    args.images_dir = args.images_dir or BACKEND_DIR / "fixtures" / (
        "urban-images" if urban else "real-images"
    )
    args.output_dir = args.output_dir or BACKEND_DIR.parent / "output" / (
        "urban-depth-evaluation" if urban else "real-depth-evaluation"
    )
    report = asyncio.run(run(args))
    print(f"Rapport : {(args.output_dir / 'report.html').resolve()}")
    print(f"Résultats : {(args.output_dir / 'results.json').resolve()}")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
