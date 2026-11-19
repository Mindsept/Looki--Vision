"""Streaming text adapters. Timers start before send, after connection setup."""

import asyncio
import json
import re
from time import perf_counter

import httpx
from websockets.asyncio.client import connect


def numeric_usage(value):
    """Keep usage counters only, never arbitrary server strings or credentials."""
    if not isinstance(value, dict):
        return {}
    return {
        key: numeric_usage(item) if isinstance(item, dict) else item
        for key, item in value.items()
        if isinstance(item, dict) or (type(item) in (int, float) and item >= 0)
    }


def error_code(event):
    code = event.get("error", {}).get("code", "provider_error")
    return str(code) if re.fullmatch(r"[a-zA-Z0-9_\-]{1,80}", str(code)) else "provider_error"


class StreamResult:
    def __init__(self):
        self.start = perf_counter()
        self.first_text_ms = None
        self.terminal_ms = None
        self.text = ""
        self.status = "stream_ended_without_terminal_event"
        self.usage = {}
        self.response_id = None
        self.reported_model = None

    def event(self, event):
        kind = event.get("type")
        if kind == "response.output_text.delta" and event.get("delta"):
            if self.first_text_ms is None:
                self.first_text_ms = (perf_counter() - self.start) * 1000
            self.text += event["delta"]
        if kind == "error":
            self.status = error_code(event)
            self.terminal_ms = (perf_counter() - self.start) * 1000
            return True
        if kind in {
            "response.done",
            "response.completed",
            "response.failed",
            "response.incomplete",
        }:
            response = event.get("response", {})
            self.status = response.get("status", "missing_status")
            if self.status == "completed" and not self.text.strip():
                self.status = "empty_text"
            self.terminal_ms = (perf_counter() - self.start) * 1000
            self.usage = numeric_usage(response.get("usage"))
            self.response_id = response.get("id")
            self.reported_model = response.get("model")
            return True
        return False

    def as_dict(self):
        return {
            "status": self.status,
            "first_text_ms": self.first_text_ms,
            "terminal_ms": self.terminal_ms,
            "elapsed_ms": (perf_counter() - self.start) * 1000,
            "text": self.text,
            "usage": self.usage,
            "response_id": self.response_id,
            "reported_model": self.reported_model,
        }


class ResponsesAdapter:
    model = "gpt-6-luna"
    transport = "https_sse"

    def __init__(self, key, timeout):
        self.timeout = timeout
        self.client = httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=1, max_keepalive_connections=1, keepalive_expiry=600
            ),
            headers={"Authorization": f"Bearer {key}"},
        )

    async def prepare(self):
        return {"setup_ms": 0, "connection_policy": "http_pool_keepalive_600s"}

    async def generate(self, content, instructions):
        result = StreamResult()
        try:
            async with asyncio.timeout(self.timeout):
                async with self.client.stream(
                    "POST",
                    "/responses",
                    json={
                        "model": self.model,
                        "stream": True,
                        "store": False,
                        "reasoning": {"effort": "low"},
                        "max_output_tokens": 1024,
                        "instructions": instructions,
                        "input": [{"role": "user", "content": content}],
                    },
                ) as response:
                    if response.status_code != 200:
                        result.status = f"http_{response.status_code}"
                    else:
                        # OpenAI emits one JSON data line per SSE event.
                        async for line in response.aiter_lines():
                            if line.startswith("data:") and line[5:].strip() != "[DONE]":
                                if result.event(json.loads(line[5:])):
                                    break
        except (TimeoutError, httpx.TimeoutException):
            result.status = "timeout"
        except (httpx.HTTPError, ValueError):
            result.status = "transport_or_decode_error"
        return result.as_dict()

    async def close(self):
        await self.client.aclose()


class RealtimeAdapter:
    model = "gpt-realtime-2.1"
    transport = "wss_text"

    def __init__(self, key, timeout):
        self.key, self.timeout = key, timeout
        self.socket = None
        self.session_model = None

    async def prepare(self):
        if self.socket is not None:
            return {"setup_ms": 0, "connection_policy": "reused_websocket"}
        start = perf_counter()
        async with asyncio.timeout(self.timeout):
            self.socket = await connect(
                f"wss://api.openai.com/v1/realtime?model={self.model}",
                additional_headers={"Authorization": f"Bearer {self.key}"},
                open_timeout=15,
                close_timeout=3,
                max_size=8 * 1024 * 1024,
            )
            await self.socket.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "type": "realtime",
                            "output_modalities": ["text"],
                            "max_output_tokens": 1024,
                            "reasoning": {"effort": "low"},
                            "tools": [],
                            "tool_choice": "none",
                        },
                    }
                )
            )
            async for raw in self.socket:
                event = json.loads(raw)
                if event.get("type") == "error":
                    raise ValueError(error_code(event))
                if event.get("type") == "session.updated":
                    self.session_model = event.get("session", {}).get("model")
                    break
            else:
                raise ValueError("connection_ended_before_session_updated")
        return {
            "setup_ms": (perf_counter() - start) * 1000,
            "connection_policy": "new_websocket",
            "session_model": self.session_model,
        }

    async def generate(self, content, instructions):
        from websockets.exceptions import WebSocketException

        result = StreamResult()
        try:
            async with asyncio.timeout(self.timeout):
                await self.socket.send(
                    json.dumps(
                        {
                            "type": "response.create",
                            "response": {
                                "conversation": "none",
                                "output_modalities": ["text"],
                                "instructions": instructions,
                                "input": [{"type": "message", "role": "user", "content": content}],
                            },
                        },
                        ensure_ascii=False,
                    )
                )
                async for raw in self.socket:
                    if result.event(json.loads(raw)):
                        break
        except TimeoutError:
            result.status = "timeout"
        except (WebSocketException, ValueError, OSError):
            result.status = "transport_or_decode_error"
        data = result.as_dict()
        if data["reported_model"] is None:
            data["reported_model"] = self.session_model
        if result.status != "completed":
            await self.close()
        return data

    async def close(self):
        if self.socket is not None:
            await self.socket.close()
            self.socket = None
