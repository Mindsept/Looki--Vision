#!/usr/bin/env python3
"""Générer trois JPEG de scénario explicites pour tester ingestion et alertes."""

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.vision.mock import MOCK_SCENES, MockVisionProvider, create_mock_image  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=BACKEND / "fixtures" / "images")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    provider = MockVisionProvider()
    manifest = {
        "simulated": True,
        "note": "Distances fixes de scénario; aucune mesure réelle.",
        "images": [],
    }
    for scene in MOCK_SCENES:
        image = create_mock_image(scene)
        path = args.output / f"{scene}.jpg"
        image.save(path, "JPEG", quality=92, subsampling=0)
        result = provider.analyze(image)
        manifest["images"].append(
            {
                "filename": path.name,
                "scene": scene,
                "expected_distances_m": [item.distance_m for item in result.detections],
            }
        )
        print(path)
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
