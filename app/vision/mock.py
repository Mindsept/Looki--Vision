"""Fixtures géométriques explicites; aucune estimation de distance sur une photo libre.

Les distances sont des valeurs de scénario, jamais une déduction physique à
partir d'une couleur ou de la taille d'un objet dans l'image. La signature et
l'ensemble de la scène doivent correspondre à une fixture connue.
"""

from typing import Literal

import numpy as np
from PIL import Image, ImageDraw

from app.vision.base import DetectionResult, VisionResult

MockScene = Literal["near", "far", "clear"]
MOCK_SCENES: tuple[MockScene, ...] = ("near", "far", "clear")
_CANONICAL_SIZE = (640, 480)
_COMPARISON_SIZE = (320, 240)
_SIGNATURE = (
    (255, 0, 220),
    (0, 220, 255),
    (255, 220, 0),
    (60, 240, 100),
    (100, 70, 250),
    (255, 130, 40),
    (0, 220, 255),
    (255, 0, 220),
)
_BOXES = {
    "near": (0.30, 0.25, 0.70, 0.90),
    "far": (0.45, 0.43, 0.55, 0.60),
}
_DISTANCES = {"near": 0.45, "far": 3.5}


def create_mock_image(scene: MockScene, size: tuple[int, int] = _CANONICAL_SIZE) -> Image.Image:
    """Construire une fixture reproductible sans fichiers ni réseau."""
    if scene not in MOCK_SCENES:
        raise ValueError(f"Unknown mock scene: {scene}")
    if min(size) < 64:
        raise ValueError("Mock images must be at least 64 × 64 pixels")
    width, height = _CANONICAL_SIZE
    image = Image.new("RGB", _CANONICAL_SIZE, (24, 32, 48))
    draw = ImageDraw.Draw(image)
    # Distinctive eight-cell signature identifies a deliberately simulated scene.
    for index, color in enumerate(_SIGNATURE):
        x = 16 + index * 76
        draw.rectangle((x, 12, x + 52, 44), fill=color)
    draw.rectangle((0, 310, width, height), fill=(50, 62, 76))
    draw.line((0, 310, 640, 310), fill=(115, 130, 145), width=3)
    draw.line((0, 479, 240, 310), fill=(115, 130, 145), width=3)
    draw.line((639, 479, 400, 310), fill=(115, 130, 145), width=3)
    if scene != "clear":
        x1, y1, x2, y2 = _BOXES[scene]
        color = (235, 60, 55) if scene == "near" else (40, 135, 245)
        draw.rectangle(
            (int(x1 * width), int(y1 * height), int(x2 * width), int(y2 * height)), fill=color
        )
    if size != _CANONICAL_SIZE:
        image = image.resize(size, Image.Resampling.LANCZOS)
    return image


class MockVisionProvider:
    """Reconnaître seulement les fixtures LOOKi, en tolérant le JPEG et le resize."""

    def __init__(self) -> None:
        self._templates = {
            scene: np.asarray(
                create_mock_image(scene).resize(_COMPARISON_SIZE, Image.Resampling.BILINEAR),
                dtype=np.float32,
            )
            for scene in MOCK_SCENES
        }

    def analyze(self, image: Image.Image) -> VisionResult:
        observed = np.asarray(
            image.convert("RGB").resize(_COMPARISON_SIZE, Image.Resampling.BILINEAR),
            dtype=np.float32,
        )
        matches: list[tuple[float, str]] = []
        for scene, template in self._templates.items():
            difference = np.abs(observed - template)
            # Checking the complete scene avoids reporting meters for an arbitrary
            # image containing a red/blue rectangle or only a copied signature.
            mean_error = float(difference.mean())
            if mean_error <= 4.0 and float(np.percentile(difference, 99)) <= 24:
                matches.append((mean_error, scene))
        if not matches:
            return VisionResult(
                provider="mock",
                detections=[],
                simulated=True,
                description="Image hors fixtures : le mode mock ne permet pas d'évaluer cette scène.",
            )
        scene = min(matches)[1]
        if scene == "clear":
            return VisionResult(
                provider="mock",
                detections=[],
                simulated=True,
                description="Scénario simulé : aucun obstacle prévu dans cette fixture.",
            )
        distance = _DISTANCES[scene]
        detection = DetectionResult(
            label="obstacle_mock",
            confidence=1.0,
            bbox=_BOXES[scene],
            distance_m=distance,
            distance_source="simulated",
        )
        return VisionResult(
            provider="mock",
            detections=[detection],
            simulated=True,
            description=f"Scénario simulé : obstacle à {distance:.2f} m.",
        )
