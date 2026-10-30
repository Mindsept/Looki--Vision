#!/usr/bin/env python3
"""Upload one real PNG capture with bounded, byte-identical retries (Pi contract v1)."""

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx


def send_frame(client, session_id, image, frame_id, timestamp, attempts=3):
    path = f"/api/v1/sessions/{session_id}/frames"
    fields = {"frame_id": str(frame_id), "timestamp": timestamp, "latest_only": "true"}
    last_error = None
    for attempt in range(attempts):
        delay = min(2**attempt, 4)
        try:
            response = client.post(
                path, data=fields, files={"image": ("capture.png", image, "image/png")}
            )
            if response.status_code in {200, 202}:
                return response.json()
            if response.status_code not in {429, 502, 503, 504}:
                response.raise_for_status()
            if response.status_code == 429:
                delay = max(delay, min(int(response.headers.get("Retry-After", "1")), 10))
            last_error = RuntimeError(f"Upload non admis : HTTP {response.status_code}")
        except httpx.TransportError as exc:
            last_error = exc
            # A timeout does not prove rejection. Consult the same UUID first.
            try:
                status = client.get(f"{path}/{frame_id}")
                if status.status_code == 200:
                    return status.json()
            except httpx.TransportError:
                pass
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise RuntimeError(
        "Upload non confirmé ; conserver UUID, timestamp et octets pour vérifier l’état."
    ) from last_error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--session-id", required=True, type=UUID)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--frame-id", type=UUID)
    parser.add_argument("--timestamp", help="UTC capture instant; required when reusing --frame-id")
    args = parser.parse_args()
    token = os.environ.get("LOOKI_PI_TOKEN")
    if not token:
        parser.error("Définir LOOKI_PI_TOKEN dans l’environnement, jamais dans l’URL.")
    if args.frame_id and not args.timestamp:
        parser.error("Un réessai avec --frame-id doit conserver --timestamp.")
    image = args.image.read_bytes()
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        parser.error("Le fichier doit être un PNG réel.")
    frame_id = args.frame_id or uuid4()
    stamp = args.timestamp or datetime.now(UTC).isoformat()
    # This example timestamps a manually supplied file. A camera loop must timestamp at capture.
    print(json.dumps({"frame_id": str(frame_id), "timestamp": stamp}), flush=True)
    with httpx.Client(
        base_url=args.base_url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=5
    ) as client:
        print(
            json.dumps(
                send_frame(client, args.session_id, image, frame_id, stamp), ensure_ascii=False
            )
        )


if __name__ == "__main__":
    main()
