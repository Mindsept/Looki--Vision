"""Public, checksum-pinned video captures; identical payloads across providers."""

import base64
import hashlib
import io
import json
import subprocess
from pathlib import Path

import httpx
from PIL import Image

from app.services.scene_context import image_input

SOURCES = {
    "MOT17-05-DPM": "29267a1a080b26ea195ba606b9c5331ad05e3db90a1eed8ed22455b0c34c00de",
    "MOT17-09-DPM": "44472faa5643b173fa9b0d4837d3018cd1f5c4a3f13ab4bc9f865cf552022f9d",
    "MOT20-01": "0105c42857c50f73c6ec50d3456f9bb8a04cbe45944bf7bbc3c77dc755feeae8",
    "MOT20-05": "e5d1099517a3d5fe8cded2035fce54ff641171b3be5b97cfcb774db90a64bc8e",
}
QUESTION = (
    "Décris brièvement les éléments visibles devant moi et leur position dans la scène récente."
)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def prepare(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    scenes = []
    for name, expected in SOURCES.items():
        url = f"https://motchallenge.net/sequenceVideos/{name}-raw.mp4"
        video = root / f"{name}.mp4"
        if not video.exists():
            part = video.with_suffix(".part")
            try:
                with httpx.stream("GET", url, timeout=120, follow_redirects=True) as stream:
                    stream.raise_for_status()
                    with part.open("wb") as dest:
                        for chunk in stream.iter_bytes():
                            dest.write(chunk)
                if sha256(part.read_bytes()) != expected:
                    raise ValueError(f"Source checksum mismatch: {name}")
                part.replace(video)
            finally:
                part.unlink(missing_ok=True)
        if sha256(video.read_bytes()) != expected:
            raise ValueError(f"Source checksum mismatch: {name}")
        frames = []
        for index, seconds in enumerate([2, 2.5, 3, 3.5, 4]):
            path = root / f"{name}-{index}.png"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-threads",
                    "1",
                    "-ss",
                    str(seconds),
                    "-i",
                    str(video),
                    "-frames:v",
                    "1",
                    "-threads",
                    "1",
                    "-y",
                    str(path),
                ],
                check=True,
                capture_output=True,
            )
            with Image.open(path) as image:
                width, height = image.size
            frames.append(
                {
                    "file": path.name,
                    "sha256": sha256(path.read_bytes()),
                    "source_time_seconds": seconds,
                    "width": width,
                    "height": height,
                }
            )
        scenes.append({"id": name, "source_url": url, "source_sha256": expected, "frames": frames})
        print(f"Prepared {name}: 5 real captures", flush=True)
    manifest = {"schema": "looki.llm-corpus.v1", "question": QUESTION, "scenes": scenes}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def payload(root, manifest, scene_id, count, quality, max_side):
    scene = next(s for s in manifest["scenes"] if s["id"] == scene_id)
    selected = {1: [4], 3: [0, 2, 4], 5: [0, 1, 2, 3, 4]}[count]
    images, frame_meta, public = [], [], []
    for ordinal, index in enumerate(selected, start=1):
        frame = scene["frames"][index]
        original = (root / frame["file"]).read_bytes()
        if sha256(original) != frame["sha256"]:
            raise ValueError(f"Frame checksum mismatch: {frame['file']}")
        encoded = image_input(original, max_side=max_side, jpeg_quality=quality)
        raw = base64.b64decode(encoded["image_url"].split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
        image_ref = f"image-{ordinal}"
        images.extend([{"type": "input_text", "text": image_ref}, encoded])
        frame_meta.append(
            {
                "image_ref": image_ref,
                "source_time_seconds": frame["source_time_seconds"],
                "width": width,
                "height": height,
            }
        )
        public.append(
            {
                "source_frame_sha256": frame["sha256"],
                "jpeg_sha256": sha256(raw),
                "jpeg_bytes": len(raw),
                "width": width,
                "height": height,
            }
        )
    context = {
        "schema_version": "looki.llm-latency-scene.v1",
        "question": manifest["question"],
        "actual_frame_count": count,
        "expected_frame_count": count,
        "perception": "not_run",
        "metric_depth": "not_available",
        "frames": frame_meta,
    }
    content = [{"type": "input_text", "text": json.dumps(context, ensure_ascii=False)}] + images
    serialized = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()
    return content, {
        "images": public,
        "jpeg_bytes": sum(f["jpeg_bytes"] for f in public),
        "content_bytes": len(serialized),
        "content_sha256": sha256(serialized),
    }
