"""Allowlisted provider measurements, without raw events, audio or provider errors."""

from app.providers.lio_prompt import PROMPT_VERSION


def generation_snapshot(settings, received_at):
    return {
        "model": settings.realtime_model,
        "voice": settings.realtime_voice,
        "prompt_version": PROMPT_VERSION,
        "reasoning_effort": settings.realtime_reasoning_effort,
        "max_output_tokens": settings.realtime_max_output_tokens,
        "requested_at": received_at.isoformat(),
    }


def token_usage(raw):
    if not isinstance(raw, dict):
        return None
    paths = {
        **{key: (key,) for key in ("total_tokens", "input_tokens", "output_tokens")},
        **{
            f"input_{key}": ("input_token_details", key)
            for key in ("text_tokens", "image_tokens", "audio_tokens", "cached_tokens")
        },
        **{
            f"cached_{key}": ("input_token_details", "cached_tokens_details", key)
            for key in ("text_tokens", "image_tokens", "audio_tokens")
        },
        **{
            f"output_{key}": ("output_token_details", key)
            for key in ("text_tokens", "audio_tokens")
        },
    }
    result = {}
    for field, path in paths.items():
        value = raw
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if type(value) is int and value >= 0:
            result[field] = value
    return result or None


def response_transcript(response):
    parts = []
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_audio":
                value = content.get("transcript")
                if isinstance(value, str):
                    parts.append(value)
    return " ".join(parts)[:8000] or None
