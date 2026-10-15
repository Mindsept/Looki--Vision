#!/usr/bin/env python3
"""Exercise the Pi → API → WebSocket chain without hardware or model downloads."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "images"


def websocket_url(base_url: str, session_id: str, after: int = 0) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = f"{parsed.path}/api/v1/sessions/{session_id}/ws"
    return urlunsplit((scheme, parsed.netloc, path, urlencode({"after": after}), ""))


def print_event(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


async def receive_until(socket, event_type: str, *, wait_seconds: float) -> dict:
    async with asyncio.timeout(wait_seconds):
        while True:
            event = json.loads(await socket.recv())
            print_event(event)
            if event["type"] == "error":
                raise RuntimeError(f"Le backend a refusé un message : {event}")
            if event["type"] == event_type:
                return event


async def run(args: argparse.Namespace) -> None:
    image_path = FIXTURE_DIR / "near.jpg"
    if not image_path.is_file():
        raise RuntimeError(
            f"Fixture absente : {image_path}. Exécutez scripts/generate_mock_images.py."
        )
    headers = {"Authorization": f"Bearer {args.api_key}"}
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"), headers=headers, timeout=args.timeout
    ) as client:
        created = await client.post(
            "/api/v1/sessions", json={"center_id": "demo-centre", "user_id": "demo-user"}
        )
        created.raise_for_status()
        session_id = created.json()["id"]
        print(f"Session créée : {session_id}", flush=True)

        async with connect(
            websocket_url(args.base_url, session_id), additional_headers=headers
        ) as socket:
            await receive_until(socket, "session.state", wait_seconds=args.timeout)
            frame_id = str(uuid4())
            uploaded = await client.post(
                f"/api/v1/sessions/{session_id}/frames",
                data={"frame_id": frame_id, "timestamp": datetime.now(UTC).isoformat()},
                files={"image": (image_path.name, image_path.read_bytes(), "image/jpeg")},
            )
            uploaded.raise_for_status()
            print(f"Image acceptée : {frame_id} ({uploaded.status_code})", flush=True)
            alert = await receive_until(socket, "alert.proximity", wait_seconds=args.timeout)
            alert_id = alert["id"]

            request_id = str(uuid4())
            await socket.send(
                json.dumps(
                    {
                        "type": "question",
                        "request_id": request_id,
                        "text": "Que vois-tu devant moi ?",
                    },
                    ensure_ascii=False,
                )
            )
            await receive_until(socket, "answer", wait_seconds=args.timeout)

        # Replay after the preceding cursor must include the alert again.
        async with connect(
            websocket_url(args.base_url, session_id, max(0, alert_id - 1)),
            additional_headers=headers,
        ) as socket:
            replayed = await receive_until(socket, "alert.proximity", wait_seconds=args.timeout)
            if replayed["id"] != alert_id:
                raise RuntimeError("Le rejeu n'a pas retrouvé la même alerte persistée.")
            print("Reconnexion vérifiée : la même alerte est rejouée.", flush=True)

        closed = await client.post(f"/api/v1/sessions/{session_id}/close")
        closed.raise_for_status()
        print("Session fermée. Démonstration terminée.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--api-key",
        default=os.getenv("LOOKI_API_KEY", "looki-local-dev-key"),
        help="Clé Bearer ; défaut : LOOKI_API_KEY ou looki-local-dev-key.",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (httpx.HTTPError, TimeoutError, OSError, RuntimeError) as exc:
        parser.exit(1, f"Échec de la démonstration : {exc}\n")


if __name__ == "__main__":
    main()
