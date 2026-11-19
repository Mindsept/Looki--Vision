"""Sequential, randomized blocks with durable per-attempt records and no retry."""

import asyncio
import itertools
import json
import platform
import random
import subprocess
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

from app.core.config import Settings
from app.providers.lio_prompt import INSTRUCTIONS, PROMPT_VERSION
from scripts.llm_benchmark.corpus import payload, sha256
from scripts.llm_benchmark.providers import RealtimeAdapter, ResponsesAdapter
from scripts.llm_benchmark.statistics import cost_upper_estimate, summarize

MODELS = (RealtimeAdapter.model, ResponsesAdapter.model)
QUALITIES = (45, 65, 85)
COUNTS = (1, 3, 5)


def schedule(repetitions, scenes, seed):
    rng = random.Random(seed)
    result = []
    for repeat in range(repetitions):
        block = list(itertools.product(MODELS, COUNTS, QUALITIES))
        rng.shuffle(block)
        for model, count, quality in block:
            result.append(
                {
                    "attempt_id": f"r{repeat:03d}-{model}-n{count}-q{quality}",
                    "phase": "measured",
                    "repeat": repeat,
                    "model": model,
                    "image_count": count,
                    "jpeg_quality": quality,
                    "scene_id": scenes[repeat % len(scenes)],
                }
            )
    return result


def timestamp():
    return datetime.now(UTC).isoformat()


def git_state():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
    return revision, dirty


def load_rows(path):
    if not path.exists():
        return []
    # Fail on a truncated last record; never silently drop a paid attempt.
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def run(args):
    key = Settings().openai_api_key.get_secret_value()
    if not key:
        raise ValueError("Set LOOKI_OPENAI_API_KEY in the private backend .env")
    manifest = json.loads((args.corpus / "manifest.json").read_text())
    scenes = [s["id"] for s in manifest["scenes"]]
    args.output.mkdir(parents=True, exist_ok=True)
    config = {
        "schema": "looki.llm-p95-run.v1",
        "models": MODELS,
        "repetitions": args.repetitions,
        "qualities": QUALITIES,
        "counts": COUNTS,
        "seed": args.seed,
        "max_side": args.max_side,
        "timeout_seconds": args.timeout,
        "max_output_tokens": 1024,
        "reasoning_effort": "low",
        "output_modality": "text",
        "concurrency": 1,
        "automatic_retries": 0,
        "warmups_per_model": args.warmups,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": sha256(INSTRUCTIONS.encode()),
        "manifest_sha256": sha256((args.corpus / "manifest.json").read_bytes()),
        "max_estimated_usd": args.max_estimated_usd,
    }
    # Normalize tuples before comparing an on-disk JSON configuration.
    config = json.loads(json.dumps(config))
    config_path = args.output / "config.json"
    if config_path.exists() and json.loads(config_path.read_text()) != config:
        raise ValueError("Existing run configuration differs; use a new output directory")
    rows_path = args.output / "requests.jsonl"
    if rows_path.exists() and not args.resume:
        raise ValueError("Output already contains requests; use --resume or a new directory")
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    if not (args.output / "environment.json").exists():
        revision, dirty = await asyncio.to_thread(git_state)
        sources = {p.name: sha256(p.read_bytes()) for p in Path(__file__).parent.glob("*.py")}
        from app.services import scene_context

        sources["app/services/scene_context.py"] = sha256(Path(scene_context.__file__).read_bytes())
        (args.output / "environment.json").write_text(
            json.dumps(
                {
                    "started_at": timestamp(),
                    "system": platform.system(),
                    "machine": platform.machine(),
                    "python": platform.python_version(),
                    "base_git_revision": revision,
                    "working_tree_dirty": dirty,
                    "source_sha256": sources,
                    "packages": {p: version(p) for p in ["httpx", "websockets", "pillow"]},
                    "deployment": "local_cli_to_openai_cloud",
                    "vm_benchmark": False,
                },
                indent=2,
            )
            + "\n"
        )
    (args.output / "prompt.txt").write_text(INSTRUCTIONS)
    (args.output / "corpus-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    prepared = {}
    for scene, count, quality in itertools.product(scenes, COUNTS, QUALITIES):
        start = perf_counter()
        content, meta = payload(args.corpus, manifest, scene, count, quality, args.max_side)
        meta["encode_ms"] = (perf_counter() - start) * 1000
        prepared[(scene, count, quality)] = (content, meta)
    (args.output / "payloads.json").write_text(
        json.dumps(
            [
                {"scene_id": scene, "image_count": count, "jpeg_quality": quality, **meta}
                for (scene, count, quality), (_, meta) in prepared.items()
            ],
            indent=2,
        )
        + "\n"
    )
    plan = [
        {
            "attempt_id": f"warmup-{model}-{i}",
            "phase": "warmup",
            "repeat": i,
            "model": model,
            "image_count": 3,
            "jpeg_quality": 85,
            "scene_id": scenes[0],
        }
        for i in range(args.warmups)
        for model in MODELS
    ]
    plan += schedule(args.repetitions, scenes, args.seed)
    (args.output / "schedule.json").write_text(json.dumps(plan, indent=2) + "\n")
    rows = load_rows(rows_path)
    done = {row["attempt_id"] for row in rows}
    adapters = {
        MODELS[0]: RealtimeAdapter(key, args.timeout),
        MODELS[1]: ResponsesAdapter(key, args.timeout),
    }
    consecutive_failures = 0
    stop_reason = "interrupted"
    try:
        for attempt in plan:
            if attempt["attempt_id"] in done:
                continue
            spent = sum(r.get("cost_upper_estimate_usd") or 0 for r in rows)
            if spent >= args.max_estimated_usd:
                stop_reason = "cost_guard_reached"
                break
            content, meta = prepared[
                (attempt["scene_id"], attempt["image_count"], attempt["jpeg_quality"])
            ]
            adapter = adapters[attempt["model"]]
            # Record intent first: a killed process must not silently retry an uncertain paid call.
            intent_path = args.output / "inflight.json"
            if intent_path.exists():
                raise ValueError("Unresolved inflight request: inspect it before resuming")
            intent_path.write_text(json.dumps({**attempt, "started_at": timestamp()}) + "\n")
            started_at = timestamp()
            try:
                setup = await adapter.prepare()
            except Exception as exc:
                # Deliberately exclude exception messages and provider bodies (may contain data).
                setup = {"setup_ms": None, "connection_policy": "setup_failed"}
                result = {
                    "status": f"setup_{type(exc).__name__}",
                    "first_text_ms": None,
                    "terminal_ms": None,
                    "elapsed_ms": None,
                    "text": "",
                    "usage": {},
                    "response_id": None,
                    "reported_model": None,
                }
                await adapter.close()
            else:
                result = await adapter.generate(content, INSTRUCTIONS)
            text = result.pop("text")
            row = {
                **attempt,
                **result,
                **setup,
                "started_at": started_at,
                "finished_at": timestamp(),
                "transport": adapter.transport,
                "jpeg_bytes": meta["jpeg_bytes"],
                "content_bytes": meta["content_bytes"],
                "content_sha256": meta["content_sha256"],
                "output_chars": len(text),
                "output_sha256": sha256(text.encode()),
                "cost_upper_estimate_usd": cost_upper_estimate(attempt["model"], result["usage"]),
            }
            with (args.output / "responses.private.jsonl").open("a") as output:
                output.write(
                    json.dumps(
                        {"attempt_id": attempt["attempt_id"], "text": text}, ensure_ascii=False
                    )
                    + "\n"
                )
            with rows_path.open("a") as output:
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()
            rows.append(row)
            intent_path.unlink()
            consecutive_failures = consecutive_failures + 1 if row["status"] != "completed" else 0
            summary = summarize(rows, args.repetitions)
            (args.output / "summary.json").write_text(
                json.dumps(summary, indent=2, allow_nan=False) + "\n"
            )
            print(
                json.dumps(
                    {
                        "done": len(rows),
                        "planned": len(plan),
                        "model": row["model"],
                        "n": row["image_count"],
                        "q": row["jpeg_quality"],
                        "status": row["status"],
                        "ttft_ms": row["first_text_ms"],
                        "total_ms": row["terminal_ms"],
                        "cost_envelope_usd": round(summary["cost_upper_estimate_usd"], 4),
                    }
                ),
                flush=True,
            )
            if consecutive_failures >= 3:
                stop_reason = "three_consecutive_failures"
                break
            # After a rate limit do not hammer the same service. No failed attempt is replaced.
            if row["status"] in {"http_429", "rate_limit_exceeded"}:
                await asyncio.sleep(5)
        else:
            stop_reason = "completed"
    finally:
        for adapter in adapters.values():
            await adapter.close()
        (args.output / "completion.json").write_text(
            json.dumps(
                {
                    "finished_at": timestamp(),
                    "stop_reason": stop_reason,
                    "recorded_requests": len(rows),
                    "planned_requests": len(plan),
                    "complete": len(rows) == len(plan),
                },
                indent=2,
            )
            + "\n"
        )
    return 0 if len(rows) == len(plan) else 2
