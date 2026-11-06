#!/usr/bin/env python3
"""Provision separate mobile/Pi capabilities into a private local file for the Lio lab."""

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--center-id", default="demo-centre")
    parser.add_argument("--output", type=Path, default=Path("output/assistant-session.json"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the file before creating a session. Never overwrite an existing set of tokens.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        with httpx.Client(
            base_url=args.base_url.rstrip("/"),
            timeout=10,
            headers={"Authorization": f"Bearer {Settings().api_key}"},
        ) as client:
            result = client.post("/api/v1/sessions", json={"center_id": args.center_id})
            result.raise_for_status()
            sid = result.json()["id"]
            json.dump({"session_id": sid}, file)
            file.flush()
            access = client.post(f"/api/v1/sessions/{sid}/access-tokens")
            access.raise_for_status()
            file.seek(0)
            json.dump(access.json(), file, indent=2)
            file.truncate()
    print(f"Accès enregistrés dans {args.output.resolve()} (permissions 0600).")
    print(
        "Ouvrir /live/?assistant=1 ; renseigner session_id et mobile_token, puis pi_token pour rejouer un JPEG."
    )


if __name__ == "__main__":
    main()
