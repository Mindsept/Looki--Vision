#!/usr/bin/env python3
"""Export complete latency runs as sanitized CSV, JSON and a standalone figure.

Run with: uv run --with matplotlib python scripts/publish_llm_benchmark.py ...
Does not read .env or make API calls. Provider transcripts and IDs stay private.
"""

import argparse
import csv
import hashlib
import itertools
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.llm_benchmark.runner import load_rows  # noqa: E402
from scripts.llm_benchmark.statistics import summarize  # noqa: E402

PUBLIC_COLUMNS = [
    "attempt_id",
    "phase",
    "repeat",
    "model",
    "reported_model",
    "transport",
    "image_count",
    "jpeg_quality",
    "scene_id",
    "status",
    "started_at",
    "finished_at",
    "first_text_ms",
    "terminal_ms",
    "elapsed_ms",
    "setup_ms",
    "connection_policy",
    "jpeg_bytes",
    "content_bytes",
    "content_sha256",
    "output_chars",
    "output_sha256",
    "cost_upper_estimate_usd",
    "usage",
]


def validate(rows, config, completion):
    expected = (
        len(config["models"])
        * len(config["counts"])
        * len(config["qualities"])
        * config["repetitions"]
    )
    counts = Counter(
        (r["model"], r["image_count"], r["jpeg_quality"]) for r in rows if r["phase"] == "measured"
    )
    if not completion["complete"] or completion["stop_reason"] != "completed":
        raise ValueError("Refusing to publish an incomplete run as final")
    if sum(counts.values()) != expected or set(counts.values()) != {config["repetitions"]}:
        raise ValueError("Missing/extra measurements in the experimental matrix")
    if set(counts) != set(
        itertools.product(config["models"], config["counts"], config["qualities"])
    ):
        raise ValueError("Unexpected experimental profiles")
    if len({r["attempt_id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate attempt IDs")
    hashes = {}
    pairs = Counter()
    for row in rows:
        if row["phase"] != "measured":
            continue
        key = (row["repeat"], row["image_count"], row["jpeg_quality"], row["scene_id"])
        pairs[key] += 1
        if key in hashes and hashes[key] != row["content_sha256"]:
            raise ValueError("The two models did not receive identical content")
        hashes[key] = row["content_sha256"]
    if set(pairs.values()) != {len(config["models"])}:
        raise ValueError("Missing paired model measurement")


def plot(summary, destination, config):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(2, 3, figsize=(13, 7), sharex=True, sharey="row", layout="constrained")
    colors = {"gpt-realtime-2.1": "#167665", "gpt-6-luna": "#7a48a5"}
    for column, quality in enumerate(config["qualities"]):
        for row, field in enumerate(["first_text_ms", "terminal_ms"]):
            ax = axes[row, column]
            for model, color in colors.items():
                cells = sorted(
                    [
                        c
                        for c in summary["cells"]
                        if c["model"] == model and c["jpeg_quality"] == quality
                    ],
                    key=lambda c: c["image_count"],
                )
                ax.plot(
                    [c["image_count"] for c in cells],
                    [
                        c[f"{field}_p95_attempts"] / 1000
                        if c[f"{field}_p95_attempts"] is not None
                        else float("nan")
                        for c in cells
                    ],
                    "o-",
                    color=color,
                    label=model,
                    linewidth=2,
                )
            ax.set_xticks([1, 3, 5])
            finite_values = [
                c[f"{field}_p95_attempts"] / 1000
                for c in summary["cells"]
                if c[f"{field}_p95_attempts"] is not None
            ]
            ax.set_ylim(0, max(finite_values, default=1) * 1.15)
            ax.grid(axis="y", alpha=0.18)
            if row == 0:
                ax.set_title(f"JPEG Q{quality} · côté long ≤ {config['max_side']} px")
            else:
                ax.set_xlabel("Nombre d’images")
            if column == 0:
                ax.set_ylabel("Premier texte · p95 (s)" if row == 0 else "Fin de réponse · p95 (s)")
    axes[0, 0].legend(loc="upper right", fontsize=9)
    fig.suptitle(
        f"LOOKi · appels OpenAI réels · {config['repetitions']} demandes par combinaison\nTexte en streaming, 1 demande à la fois · ne mesure pas le son entendu",
        fontsize=14,
    )
    fig.savefig(destination / "p95.png", dpi=170)
    fig.savefig(destination / "p95.svg")
    plt.close(fig)


def markdown_tables(summary, rows, config):
    """Keep the human-readable tables derived from the same immutable records."""

    def duration(value):
        return f"{value / 1000:.3f}".replace(".", ",") if value is not None else "non borné"

    cells = {(c["model"], c["image_count"], c["jpeg_quality"]): c for c in summary["cells"]}
    output = [
        "## Résultats mesurés",
        "",
        f"**{summary['measured_requests']} demandes mesurées**, "
        f"{config['repetitions']} par combinaison. "
        f"{sum(c['failures'] for c in summary['cells'])} échec(s). "
        f"Les {sum(r['phase'] == 'warmup' for r in rows)} demandes de mise en route sont exclues des percentiles.",
        "",
        "Durées en **secondes**. Le p50 porte sur les réponses complètes ; le p95 porte sur toutes les tentatives (échec = +∞).",
        "",
        "| JPEG | Images | Realtime premier texte p50 / p95 | Luna premier texte p50 / p95 | Realtime fin p50 / p95 | Luna fin p50 / p95 | Réussites RT / Luna |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for quality in config["qualities"]:
        for count in config["counts"]:
            rt, luna = (cells[(model, count, quality)] for model in config["models"])
            values = [
                f"{duration(c[f'{field}_p50_success'])} / {duration(c[f'{field}_p95_attempts'])}"
                for field in ["first_text_ms", "terminal_ms"]
                for c in [rt, luna]
            ]
            output.append(
                f"| {quality} | {count} | "
                + " | ".join(values)
                + f" | {rt['successes']}/{rt['attempts']} · {luna['successes']}/{luna['attempts']} |"
            )
    output += [
        "",
        "### Taille des entrées",
        "",
        "Moyenne par demande, somme des JPEG avant base64, en **kio** (1 024 octets). Les deux modèles reçoivent les mêmes contenus. Le base64 ajoute environ un tiers, puis viennent le JSON et les instructions.",
        "",
        "| Qualité | 1 image | 3 images | 5 images |",
        "| --- | ---: | ---: | ---: |",
    ]
    for quality in config["qualities"]:
        values = [
            f"{cells[(config['models'][0], n, quality)]['jpeg_bytes_mean'] / 1024:.1f}".replace(
                ".", ","
            )
            for n in config["counts"]
        ]
        output.append(f"| {quality} | " + " | ".join(values) + " |")
    output += [
        "",
        "### Cache et consommation",
        "",
        "Tokens agrégés sur les seules demandes mesurées. Les tokens de sortie incluent le raisonnement compté par le fournisseur. Les compteurs de cache documentent une campagne répétée ; ils ne prouvent pas un démarrage à froid.",
        "",
        "| Modèle | Entrée | Entrée en cache | Sortie | Demandes avec usage |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for model in config["models"]:
        selected = [r for r in rows if r["phase"] == "measured" and r["model"] == model]
        cached = sum(
            (
                r["usage"].get("input_token_details")
                or r["usage"].get("input_tokens_details")
                or {}
            ).get("cached_tokens", 0)
            for r in selected
        )
        output.append(
            f"| `{model}` | {sum(r['usage'].get('input_tokens', 0) for r in selected)} | {cached} | "
            f"{sum(r['usage'].get('output_tokens', 0) for r in selected)} | {sum(bool(r['usage']) for r in selected)}/{len(selected)} |"
        )
    output += [
        "",
        f"Enveloppe tarifaire prudente calculée : **{summary['cost_upper_estimate_usd']:.3f} USD** pour les {len(rows)} appels de cette campagne, mise en route incluse, essai préliminaire exclu. "
        f"Usage/coût manquant pour {summary['requests_with_unknown_cost']} demande(s). Ce montant sans remise de cache n’est pas une facture OpenAI.",
        "",
    ]
    return "\n".join(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads((args.output / "config.json").read_text())
    completion = json.loads((args.output / "completion.json").read_text())
    rows = load_rows(args.output / "requests.jsonl")
    validate(rows, config, completion)
    args.destination.mkdir(parents=True, exist_ok=True)
    summary = summarize(rows, config["repetitions"])
    (args.destination / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False) + "\n"
    )
    (args.destination / "tables.md").write_text(markdown_tables(summary, rows, config))
    with (args.destination / "requests.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=PUBLIC_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "usage": json.dumps(row["usage"], separators=(",", ":"))})
    for name in [
        "config.json",
        "environment.json",
        "corpus-manifest.json",
        "payloads.json",
        "schedule.json",
        "completion.json",
        "prompt.txt",
    ]:
        (args.destination / name).write_bytes((args.output / name).read_bytes())
    plot(summary, args.destination, config)
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(args.destination.iterdir())
        if path.is_file() and path.name != "sha256.json"
    }
    (args.destination / "sha256.json").write_text(json.dumps(hashes, indent=2) + "\n")
    print(
        json.dumps(
            {
                "measurements": summary["measured_requests"],
                "files": len(hashes),
                "destination": str(args.destination),
            }
        )
    )


if __name__ == "__main__":
    main()
