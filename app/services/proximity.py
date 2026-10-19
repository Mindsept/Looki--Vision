import math

from app.core.config import Settings

# Canonical COCO labels are preserved in the API; this small presentation
# dictionary keeps common outdoor warnings suitable for the iPhone's French TTS.
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
