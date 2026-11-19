import base64
import io
import json
from collections import Counter

import httpx
import pytest
from PIL import Image

from app.services.scene_context import image_input
from scripts.llm_benchmark.corpus import payload, sha256
from scripts.llm_benchmark.providers import ResponsesAdapter, StreamResult, numeric_usage
from scripts.llm_benchmark.runner import schedule
from scripts.llm_benchmark.statistics import percentile, summarize
from scripts.publish_llm_benchmark import validate


def test_nearest_rank_and_failed_attempts_are_not_hidden():
    assert percentile(list(range(1, 101)), 0.95) == 95
    assert percentile([], 0.95) is None
    rows = [
        {
            "phase": "measured",
            "model": "gpt-6-luna",
            "image_count": 3,
            "jpeg_quality": 85,
            "status": "completed" if i < 94 else "timeout",
            "first_text_ms": i if i < 94 else None,
            "terminal_ms": i + 100 if i < 94 else None,
            "jpeg_bytes": 500,
            "output_chars": 100,
            "usage": {},
            "cost_upper_estimate_usd": None,
        }
        for i in range(100)
    ]
    result = summarize(rows, 100)
    cell = result["cells"][0]
    assert cell["attempts"] == 100 and cell["successes"] == 94 and cell["failures"] == 6
    assert cell["first_text_ms_p95_success"] == 89
    assert cell["first_text_ms_p95_attempts"] is None
    assert cell["first_text_ms_p95_attempts_unbounded"]
    assert result["requests_with_unknown_cost"] == 100
    json.dumps(result, allow_nan=False)


def test_schedule_is_balanced_reproducible_and_interleaves_models():
    scenes = ["a", "b", "c", "d"]
    plan = schedule(100, scenes, 42)
    assert plan == schedule(100, scenes, 42)
    assert plan != schedule(100, scenes, 43)
    assert len({p["attempt_id"] for p in plan}) == 1800
    assert set(
        Counter((p["model"], p["image_count"], p["jpeg_quality"]) for p in plan).values()
    ) == {100}
    assert set(
        Counter(
            (p["model"], p["image_count"], p["jpeg_quality"], p["scene_id"]) for p in plan
        ).values()
    ) == {25}
    assert {p["model"] for p in plan[:18]} == {"gpt-6-luna", "gpt-realtime-2.1"}


def test_stream_result_times_first_nonempty_text_and_terminal_separately(monkeypatch):
    clock = iter([10, 10.25, 11])
    monkeypatch.setattr("scripts.llm_benchmark.providers.perf_counter", lambda: next(clock))
    result = StreamResult()
    assert not result.event({"type": "response.created"})
    assert not result.event({"type": "response.output_text.delta", "delta": ""})
    assert not result.event({"type": "response.output_text.delta", "delta": "Bonjour"})
    assert not result.event({"type": "response.output_text.delta", "delta": " !"})
    assert result.event(
        {
            "type": "response.done",
            "response": {"status": "completed", "usage": {"input_tokens": 12}},
        }
    )
    assert result.first_text_ms == 250
    assert result.terminal_ms == 1000
    assert result.text == "Bonjour !" and result.status == "completed"


def test_incomplete_or_empty_output_is_not_a_success():
    empty = StreamResult()
    empty.event({"type": "response.completed", "response": {"status": "completed"}})
    assert empty.status == "empty_text"
    partial = StreamResult()
    partial.event({"type": "response.output_text.delta", "delta": "Début"})
    partial.event({"type": "response.incomplete", "response": {"status": "incomplete"}})
    assert partial.status == "incomplete"
    assert partial.first_text_ms is not None


def test_usage_only_preserves_nonnegative_numeric_counters():
    assert numeric_usage(
        {
            "input_tokens": 12,
            "secret": "do-not-publish",
            "invalid": -2,
            "flag": True,
            "input_tokens_details": {"cached_tokens": 3},
        }
    ) == {"input_tokens": 12, "input_tokens_details": {"cached_tokens": 3}}


@pytest.mark.parametrize("kwargs", [{"jpeg_quality": 96}, {"max_side": 0}])
def test_image_profile_is_validated(kwargs):
    with pytest.raises(ValueError):
        image_input(b"", **kwargs)


def test_payloads_share_latest_frame_and_jpeg_hashes(tmp_path):
    frames = []
    for index in range(5):
        path = tmp_path / f"{index}.png"
        Image.new("RGB", (1600, 900), (index * 30, 40, 100)).save(path)
        frames.append(
            {
                "file": path.name,
                "sha256": sha256(path.read_bytes()),
                "source_time_seconds": index / 2,
            }
        )
    manifest = {"question": "Décris", "scenes": [{"id": "test", "frames": frames}]}
    variants = [payload(tmp_path, manifest, "test", count, 65, 768) for count in [1, 3, 5]]
    assert len({meta["images"][-1]["jpeg_sha256"] for _, meta in variants}) == 1
    for (content, meta), count in zip(variants, [1, 3, 5], strict=True):
        assert len([c for c in content if c["type"] == "input_image"]) == count
        raw = base64.b64decode(content[-1]["image_url"].split(",")[1])
        with Image.open(io.BytesIO(raw)) as image:
            assert image.size == (768, 432) and image.format == "JPEG"
        assert meta["images"][-1]["jpeg_sha256"] == sha256(raw)
    (tmp_path / "4.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        payload(tmp_path, manifest, "test", 1, 65, 768)


async def test_responses_adapter_sends_real_payload_without_history_and_parses_sse():
    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        events = [
            {"type": "response.output_text.delta", "delta": "Des piétons."},
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "id": "resp-test",
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                },
            },
        ]
        return httpx.Response(200, text="".join(f"data: {json.dumps(e)}\n\n" for e in events))

    adapter = ResponsesAdapter("unused-test-key", 5)
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(
        base_url="https://example.test", transport=httpx.MockTransport(handle)
    )
    try:
        result = await adapter.generate(
            [{"type": "input_text", "text": "real test payload"}], "French"
        )
    finally:
        await adapter.close()
    assert result["status"] == "completed" and result["text"] == "Des piétons."
    assert result["terminal_ms"] >= result["first_text_ms"] >= 0
    assert captured[0]["store"] is False and "previous_response_id" not in captured[0]
    assert captured[0]["reasoning"] == {"effort": "low"}


async def test_http_errors_are_recorded_without_leaking_provider_body():
    adapter = ResponsesAdapter("unused-test-key", 5)
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(
        base_url="https://example.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, text="sensitive provider error body")
        ),
    )
    try:
        result = await adapter.generate([], "French")
    finally:
        await adapter.close()
    assert result["status"] == "http_429"
    assert "sensitive" not in json.dumps(result)


def test_publication_rejects_incomplete_runs_and_mismatched_inputs():
    config = {"models": ["a", "b"], "counts": [1], "qualities": [85], "repetitions": 1}
    complete = {"complete": True, "stop_reason": "completed"}
    rows = [
        {
            "attempt_id": model,
            "phase": "measured",
            "repeat": 0,
            "model": model,
            "image_count": 1,
            "jpeg_quality": 85,
            "scene_id": "a",
            "content_sha256": "same",
        }
        for model in ["a", "b"]
    ]
    validate(rows, config, complete)
    with pytest.raises(ValueError, match="incomplete"):
        validate(rows, config, {"complete": False, "stop_reason": "interrupted"})
    with pytest.raises(ValueError, match="Missing"):
        validate(rows[:1], config, complete)
    rows[1]["content_sha256"] = "different"
    with pytest.raises(ValueError, match="identical"):
        validate(rows, config, complete)


async def test_realtime_adapter_is_stateless_text_only_and_returns_usage():
    from scripts.llm_benchmark.providers import RealtimeAdapter

    class Socket:
        def __init__(self):
            self.sent = []
            self.events = iter(
                [
                    {"type": "response.output_text.delta", "delta": "Une rue."},
                    {
                        "type": "response.done",
                        "response": {
                            "status": "completed",
                            "usage": {
                                "input_tokens": 100,
                                "output_tokens": 4,
                                "input_token_details": {"image_tokens": 80, "cached_tokens": 0},
                            },
                        },
                    },
                ]
            )

        async def send(self, value):
            self.sent.append(json.loads(value))

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return json.dumps(next(self.events))
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        async def close(self):
            pass

    adapter = RealtimeAdapter("unused-test-key", 5)
    adapter.socket = Socket()
    content = [
        {"type": "input_text", "text": "question"},
        {"type": "input_image", "image_url": "data:image/jpeg;base64,test"},
    ]
    result = await adapter.generate(content, "French")
    request = adapter.socket.sent[0]["response"]
    assert request["conversation"] == "none" and request["output_modalities"] == ["text"]
    assert request["input"][0]["content"] == content
    assert result["status"] == "completed" and result["usage"]["input_tokens"] == 100
    assert result["first_text_ms"] <= result["terminal_ms"]
    await adapter.close()
