#!/usr/bin/env python3
"""Check local Lio configuration, optionally model access. Never generates audio."""

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.providers.lio_prompt import PROMPT_VERSION  # noqa: E402


def check(settings, remote=False, transport=None):
    configured = bool(settings.openai_api_key.get_secret_value())
    result = {
        "enabled": settings.realtime_enabled,
        "key_configured": configured,
        "model": settings.realtime_model,
        "voice": settings.realtime_voice,
        "prompt_version": PROMPT_VERSION,
        "reasoning_effort": settings.realtime_reasoning_effort,
        "max_output_tokens": settings.realtime_max_output_tokens,
        "provider_check": "not_run",
        "real_audio_validated": False,
    }
    if remote:
        if not configured:
            result["provider_check"] = "missing_key"
            return result, False
        try:
            with httpx.Client(
                base_url="https://api.openai.com/v1",
                timeout=10,
                transport=transport,
                headers={"Authorization": f"Bearer {settings.openai_api_key.get_secret_value()}"},
            ) as client:
                # Quoting prevents a configured model name from altering the endpoint.
                from urllib.parse import quote

                response = client.get(f"/models/{quote(settings.realtime_model, safe='')}")
            if response.status_code != 200:
                result["provider_check"] = f"http_{response.status_code}"
                return result, False
            result["provider_check"] = "model_accessible"
        except httpx.HTTPError:
            result["provider_check"] = "network_unavailable"
            return result, False
    return result, True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Read model metadata with the server key; no generation.",
    )
    args = parser.parse_args()
    result, ok = check(Settings(), args.remote)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
