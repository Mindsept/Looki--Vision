import math
from datetime import timedelta

from app.core.config import Settings
from app.core.time import as_utc

# Canonical COCO labels are preserved in the API; this small presentation
# dictionary supplies display/debug text. Mobile alerts use prerecorded sound_id clips.
FRENCH_OBJECT_NAMES = {
    "person": "personne",
    "bicycle": "vélo",
    "car": "voiture",
    "motorcycle": "moto",
    "bus": "bus",
    "truck": "camion",
    "train": "train",
    "traffic light": "feu de circulation",
    "fire hydrant": "borne incendie",
    "stop sign": "panneau stop",
    "parking meter": "parcmètre",
    "bench": "banc",
    "dog": "chien",
    "cat": "chat",
    "backpack": "sac à dos",
    "umbrella": "parapluie",
    "suitcase": "valise",
    "chair": "chaise",
    "bottle": "bouteille",
    "skateboard": "skateboard",
}


class ProximityService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def audio_fields(self, captured_at, detections):
        clips = set()
        for detection in detections:
            label = detection["label"]
            center = (detection["bbox"][0] + detection["bbox"][2]) / 2
            direction = "left" if center < 1 / 3 else "right" if center > 2 / 3 else "front"
            clips.add(
                f"v1.{label}_{direction}"
                if label in {"person", "bicycle", "car"}
                else "v1.proximity_generic"
            )
        return {
            "expires_at": (
                as_utc(captured_at) + timedelta(seconds=self.settings.alert_ttl_seconds)
            ).isoformat(),
            "sound_id": next(iter(clips)) if len(clips) == 1 else "v1.proximity_generic",
        }

    def classify(self, distance_m: float | None, confidence: float) -> str:
        if (
            distance_m is None
            or not math.isfinite(distance_m)
            or distance_m <= 0
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
            or confidence < self.settings.min_confidence
        ):
            return "unknown"
        if distance_m <= self.settings.near_threshold_m:
            return "near"
        if distance_m >= self.settings.far_threshold_m:
            return "far"
        return "medium"

    @staticmethod
    def warning_text(close: list[dict], *, simulated: bool) -> str:
        """One warning per frame; its payload retains every nearby detection."""
        nearest = min(close, key=lambda item: item["distance_m"])
        prefix = "Simulation : " if simulated else ""
        distance = nearest["distance_m"]
        if nearest.get("observation_type") != "object":
            return f"{prefix}attention, obstacle proche, à environ {distance:.2f} mètre."
        name = FRENCH_OBJECT_NAMES.get(nearest["label"], "objet")
        x1, _, x2, _ = nearest["bbox"]
        center = (x1 + x2) / 2
        direction = "à gauche" if center < 1 / 3 else "à droite" if center > 2 / 3 else "devant"
        return f"{prefix}Attention, {name} {direction}, à environ {distance:.2f} mètre."

    @staticmethod
    def possible_warning_text(detections: list[dict]) -> str:
        objects = []
        for item in detections:
            name = FRENCH_OBJECT_NAMES.get(item["label"], "objet")
            center = (item["bbox"][0] + item["bbox"][2]) / 2
            direction = "à gauche" if center < 1 / 3 else "à droite" if center > 2 / 3 else "devant"
            wording = f"{name} {direction}"
            if wording not in objects:
                objects.append(wording)
        return (
            "Proximité possible : "
            + ", ".join(objects)
            + ". Vigilance visuelle, distance incertaine."
        )
