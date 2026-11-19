"""Nearest-rank quantiles; failed requests stay visible in attempt-level results."""

import math
from collections import Counter, defaultdict


def percentile(values, q):
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(q * len(values)) - 1)]


def cost_upper_estimate(model, usage):
    """Uncached standard list-price envelope, USD; not a provider invoice.

    Realtime input uses the higher image rate for all text/image tokens. Luna
    input uses the cache-write rate. This excludes regional/service-tier premiums.
    Missing usage cannot be interpreted as zero cost.
    """
    if "input_tokens" not in usage or "output_tokens" not in usage:
        return None
    input_rate, output_rate = (5, 24) if model == "gpt-realtime-2.1" else (0.125, 0.5)
    return (usage["input_tokens"] * input_rate + usage["output_tokens"] * output_rate) / 1e6


def summarize(rows, expected_per_cell):
    grouped = defaultdict(list)
    for row in rows:
        if row["phase"] == "measured":
            grouped[(row["model"], row["image_count"], row["jpeg_quality"])].append(row)
    cells = []
    for (model, count, quality), items in sorted(grouped.items()):
        success = [r for r in items if r["status"] == "completed"]
        cell = {
            "model": model,
            "image_count": count,
            "jpeg_quality": quality,
            "attempts": len(items),
            "expected": expected_per_cell,
            "successes": len(success),
            "failures": len(items) - len(success),
            "statuses": dict(Counter(r["status"] for r in items)),
            "jpeg_bytes_mean": sum(r["jpeg_bytes"] for r in items) / len(items),
            "output_chars_mean": sum(r["output_chars"] for r in success) / len(success)
            if success
            else None,
            "input_tokens_mean": _mean_usage(items, "input_tokens"),
            "output_tokens_mean": _mean_usage(items, "output_tokens"),
            "cached_input_tokens_mean": _mean_cached(items),
        }
        for field in ["first_text_ms", "terminal_ms"]:
            available = [r[field] for r in success if r[field] is not None]
            cell[f"{field}_success_n"] = len(available)
            for q, label in [(0.5, "p50"), (0.95, "p95")]:
                cell[f"{field}_{label}_success"] = percentile(available, q)
            # An unsuccessful request cannot improve the all-attempt latency SLO.
            attempted = [
                r[field] if r["status"] == "completed" and r[field] is not None else math.inf
                for r in items
            ]
            value = percentile(attempted, 0.95)
            cell[f"{field}_p95_attempts"] = value if math.isfinite(value) else None
            cell[f"{field}_p95_attempts_unbounded"] = not math.isfinite(value)
        cells.append(cell)
    costs = [
        r["cost_upper_estimate_usd"] for r in rows if r.get("cost_upper_estimate_usd") is not None
    ]
    return {
        "quantile_method": "nearest_rank_ceil_p_n",
        "cells": cells,
        "recorded_requests": len(rows),
        "measured_requests": sum(r["phase"] == "measured" for r in rows),
        "cost_upper_estimate_usd": sum(costs),
        "requests_with_unknown_cost": sum(r.get("cost_upper_estimate_usd") is None for r in rows),
    }


def _mean_usage(rows, field):
    values = [r["usage"][field] for r in rows if field in r["usage"]]
    return sum(values) / len(values) if values else None


def _mean_cached(rows):
    values = [
        r["usage"][
            "input_token_details" if "input_token_details" in r["usage"] else "input_tokens_details"
        ].get("cached_tokens", 0)
        for r in rows
        if "input_token_details" in r["usage"] or "input_tokens_details" in r["usage"]
    ]
    return sum(values) / len(values) if values else None
