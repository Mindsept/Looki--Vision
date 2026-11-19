#!/usr/bin/env python3
"""Real OpenAI image-to-text latency benchmark (paid calls only with the run command)."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.llm_benchmark.corpus import prepare  # noqa: E402
from scripts.llm_benchmark.runner import load_rows, run  # noqa: E402
from scripts.llm_benchmark.statistics import summarize  # noqa: E402


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    corpus = commands.add_parser("prepare", help="Download checksum-pinned public video captures")
    corpus.add_argument("--corpus", type=Path, default=Path("output/llm-p95/corpus"))
    bench = commands.add_parser(
        "run", help="Make real, billable API calls with the private server key"
    )
    bench.add_argument("--corpus", type=Path, default=Path("output/llm-p95/corpus"))
    bench.add_argument("--output", type=Path, required=True)
    bench.add_argument("--repetitions", type=positive_int, default=100)
    bench.add_argument("--warmups", type=positive_int, default=1)
    bench.add_argument("--max-side", type=int, default=768)
    bench.add_argument("--seed", type=int, default=20261119)
    bench.add_argument("--timeout", type=positive_int, default=30)
    bench.add_argument("--max-estimated-usd", type=float, default=25)
    bench.add_argument("--resume", action="store_true")
    report = commands.add_parser("summarize", help="Recompute statistics without API calls")
    report.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.corpus)
    elif args.command == "run":
        if args.max_estimated_usd <= 0:
            parser.error("--max-estimated-usd must be positive")
        return asyncio.run(run(args))
    else:
        config = json.loads((args.output / "config.json").read_text())
        result = summarize(load_rows(args.output / "requests.jsonl"), config["repetitions"])
        (args.output / "summary.json").write_text(
            json.dumps(result, indent=2, allow_nan=False) + "\n"
        )
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
