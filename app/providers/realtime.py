"""OpenAI Realtime GA signaling/control only. Audio goes directly to WebRTC."""

import json
import re
from urllib.parse import urlparse

import httpx
from websockets.asyncio.client import connect

from app.core.errors import DomainError
from app.providers.lio_prompt import INSTRUCTIONS, PROMPT_VERSION, instructions_for_scene


class OpenAIRealtimeProvider:
    def __init__(self, settings):
        self.settings = settings
        self.http = httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            timeout=settings.realtime_connect_timeout_seconds,
            headers={"Authorization": f"Bearer {settings.openai_api_key.get_secret_value()}"},
        )

    def configuration(self):
        return {
            "type": "realtime",
            "model": self.settings.realtime_model,
            "output_modalities": ["audio"],
            "instructions": INSTRUCTIONS,
            "max_output_tokens": self.settings.realtime_max_output_tokens,
            "reasoning": {"effort": self.settings.realtime_reasoning_effort},
            "tools": [],
            "tool_choice": "none",
            "audio": {
                "input": {"turn_detection": None},
                "output": {"voice": self.settings.realtime_voice},
            },
        }

    async def create(self, offer):
        try:
            response = await self.http.post(
                "/realtime/calls",
                files={
                    "sdp": (None, offer, "application/sdp"),
                    "session": (None, json.dumps(self.configuration()), "application/json"),
                },
            )
            response.raise_for_status()
            path = urlparse(response.headers.get("location", "")).path
            match = re.fullmatch(r"/v1/realtime/calls/([A-Za-z0-9_-]+)", path)
            if not match or not response.text.startswith("v=0"):
                raise ValueError("Invalid signaling response")
            return match.group(1), response.text
        except (httpx.HTTPError, ValueError):
            raise DomainError(
                503, "provider_unavailable", "Négociation Realtime indisponible."
            ) from None

    async def attach(self, call_id):
        socket = await connect(
            f"wss://api.openai.com/v1/realtime?call_id={call_id}",
            additional_headers={
                "Authorization": f"Bearer {self.settings.openai_api_key.get_secret_value()}"
            },
            open_timeout=self.settings.realtime_connect_timeout_seconds,
            max_size=4 * 1024 * 1024,
        )
        try:
            config = self.configuration()
            config.pop("model")
            await socket.send(json.dumps({"type": "session.update", "session": config}))
            # The caller bounds this handshake with asyncio.timeout().
            async for raw in socket:
                event = json.loads(raw)
                if event.get("type") == "session.updated":
                    return socket
                if event.get("type") == "error":
                    break
            raise DomainError(503, "control_unavailable", "Contrôle Realtime indisponible.")
        except BaseException:
            await socket.close()
            raise

    async def generate(self, socket, question, images):
        context = question.context
        content = [{"type": "input_text", "text": json.dumps(context, ensure_ascii=False)}]
        for frame, image in zip(context["frames"], images, strict=True):
            content.extend([{"type": "input_text", "text": frame["image_ref"]}, image])
        await socket.send(
            json.dumps(
                {
                    "type": "response.create",
                    "event_id": f"question_{question.request_id}",
                    "response": {
                        "conversation": "none",
                        "output_modalities": ["audio"],
                        "max_output_tokens": self.settings.realtime_max_output_tokens,
                        "reasoning": {"effort": self.settings.realtime_reasoning_effort},
                        "instructions": instructions_for_scene(context),
                        "tools": [],
                        "tool_choice": "none",
                        "metadata": {
                            "request_id": str(question.request_id),
                            "context_id": str(question.context_id),
                            "prompt_version": PROMPT_VERSION,
                        },
                        "input": [{"type": "message", "role": "user", "content": content}],
                    },
                }
            )
        )

    async def interrupt(self, socket, response_id):
        if response_id:
            await socket.send(json.dumps({"type": "response.cancel", "response_id": response_id}))
        await socket.send(json.dumps({"type": "output_audio_buffer.clear"}))

    async def hangup(self, call_id):
        response = await self.http.post(f"/realtime/calls/{call_id}/hangup")
        if response.status_code not in {200, 204, 404, 410}:
            raise DomainError(503, "control_unavailable", "Fermeture fournisseur non confirmée.")

    async def close(self):
        await self.http.aclose()
