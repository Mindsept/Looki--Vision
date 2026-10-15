"""Port d'analyse indépendant des transports HTTP, du stockage et des modèles IA."""

import math
from dataclasses import dataclass, field
from typing import Literal, Protocol

from PIL import Image


@dataclass(frozen=True)
class DetectionResult:
    """Une observation et sa distance; ``bbox`` utilise des coordonnées xyxy [0, 1].

    Pour le provider de profondeur, la bbox est une zone d'échantillonnage,
    et ``confidence`` représente la couverture de pixels valides, pas une
    probabilité de reconnaissance d'objet ou une précision métrique calibrée.
    Pour ``urban_objects``, ``confidence`` est le score du détecteur et
    ``depth_valid_fraction`` porte séparément la couverture de profondeur.
    """

    label: str
    confidence: float
    bbox: tuple[float, float, float, float]
    distance_m: float | None
    distance_source: Literal["simulated", "metric_model"] = "simulated"
    observation_type: Literal["region", "object"] = "region"
    depth_valid_fraction: float | None = None

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("Detection label must not be empty")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("Detection confidence must be finite and between 0 and 1")
        if len(self.bbox) != 4 or not all(math.isfinite(v) and 0 <= v <= 1 for v in self.bbox):
            raise ValueError("Detection bbox must contain four finite normalized values")
        x1, y1, x2, y2 = self.bbox
        if x1 >= x2 or y1 >= y2:
            raise ValueError("Detection bbox must have positive width and height")
        if self.distance_m is not None and (
            not math.isfinite(self.distance_m) or self.distance_m <= 0
        ):
            raise ValueError("Detection distance must be positive and finite or None")
        if self.distance_source not in ("simulated", "metric_model"):
            raise ValueError("Unsupported distance source")
        if self.observation_type not in ("region", "object"):
            raise ValueError("Unsupported observation type")
        if self.depth_valid_fraction is not None and (
            not math.isfinite(self.depth_valid_fraction) or not 0 <= self.depth_valid_fraction <= 1
        ):
            raise ValueError("Depth valid fraction must be finite and between 0 and 1")


@dataclass(frozen=True)
class VisionResult:
    """Résultat explicite: une simulation n'est jamais présentée comme une mesure."""

    provider: str
    detections: list[DetectionResult]
    description: str
    simulated: bool
    timing_ms: dict[str, float] = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


class VisionProvider(Protocol):
    """Port synchrone à exécuter hors de la boucle asyncio par le worker."""

    def analyze(self, image: Image.Image) -> VisionResult:
        """Analyser une image RGB décodée, sans accès à la base de données."""
        ...
