#!/usr/bin/env python3
"""Verify published file hashes and recompute statistics from CSV, offline.

Uses the Python standard library only. No .env, API, models or database needed.
"""

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.llm_benchmark.statistics import summarize  # noqa: E402


def verify(directory):
    directory = directory.resolve()
    hashes = json.loads((directory / "sha256.json").read_text())
    for name, expected in hashes.items():
        path = (directory / name).resolve()
        if path.parent != directory or not path.is_file():
            raise ValueError(f"Invalid or missing result file: {name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Checksum mismatch: {name}")
    required = {"requests.csv", "config.json", "summary.json", "completion.json"}
    if not required <= hashes.keys():
        raise ValueError("Required results are missing from the checksum manifest")
    rows = []
    with (directory / "requests.csv").open(newline="") as source:
        for row in csv.DictReader(source):
            for field in [
                "repeat",
                "image_count",
                "jpeg_quality",
                "output_chars",
                "jpeg_bytes",
                "content_bytes",
            ]:
                row[field] = int(row[field])
            for field in [
                "first_text_ms",
                "terminal_ms",
                "elapsed_ms",
                "setup_ms",
                "cost_upper_estimate_usd",
            ]:
                row[field] = float(row[field]) if row[field] else None
            row["usage"] = json.loads(row["usage"])
            rows.append(row)
    config = json.loads((directory / "config.json").read_text())
    completion = json.loads((directory / "completion.json").read_text())
    if not completion["complete"] or completion["recorded_requests"] != len(rows):
        raise ValueError("The published campaign is incomplete")
    if len({r["attempt_id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate attempts in the CSV")
    computed = summarize(rows, config["repetitions"])
    if computed != json.loads((directory / "summary.json").read_text()):
        raise ValueError("CSV does not reproduce the published statistics")
    return {
        "verified_files": len(hashes),
        "verified_requests": len(rows),
        "measured_requests": computed["measured_requests"],
        "statistics_match": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.directory)))


if __name__ == "__main__":
    main()
