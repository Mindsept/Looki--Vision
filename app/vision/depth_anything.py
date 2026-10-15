"""Inférence locale Depth Anything V2 *metric*, sans normalisation des distances.

Le modèle fournit une carte de profondeur et aucun nom d'objet. Pour ce POC,
trois zones fixes produisent des observations d'obstacles génériques; leur bbox
représente la zone échantillonnée, pas une boîte issue d'un détecteur sémantique.
Une future implémentation peut combiner cette carte avec un détecteur dédié.

Référence officielle :
https://huggingface.co/depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf
"""

import math
import threading
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

import numpy as np
from PIL import Image

from app.vision.base import DetectionResult, VisionResult
from app.vision.timing import elapsed_ms, synchronize_device

DEFAULT_MODEL_ID = "depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf"
DEFAULT_OUTDOOR_MODEL_ID = "depth-anything/Depth-Anything-V2-Metric-Outdoor-Small-hf"
REGIONS = (
    ("obstacle_left", (0.05, 0.20, 0.35, 0.80)),
    ("obstacle_center", (0.35, 0.20, 0.65, 0.80)),
    ("obstacle_right", (0.65, 0.20, 0.95, 0.80)),
)


@dataclass(frozen=True)
class DepthPrediction:
    """Carte brute en mètres à résolution native, sans normalisation d'affichage."""

    depth_m: np.ndarray
    timing_ms: dict[str, float]
    metadata: dict[str, Any] = field(default_factory=dict)


def validate_metric_config(config: Any) -> float:
    """Refuser les poids relatifs: leurs nombres ne sont pas des mètres."""
    if getattr(config, "model_type", None) not in {"depth_anything", "depth_anything_v2"}:
        raise ValueError("Expected a Depth Anything model configuration")
    if getattr(config, "depth_estimation_type", None) != "metric":
        raise ValueError("Depth Anything must use metric weights; relative depth is not meters")
    max_depth = getattr(config, "max_depth", None)
    if not isinstance(max_depth, (float, int)) or not math.isfinite(max_depth) or max_depth <= 0:
        raise ValueError("Metric model configuration requires a finite positive max_depth")
    return float(max_depth)


def summarize_metric_depth(depth: np.ndarray, max_depth_m: float) -> list[DetectionResult]:
    """Résumer les valeurs brutes en mètres, en ignorant NaN/Inf/zéros/hors plage.

    Le percentile 10 détecte une surface proche occupant une part de la zone sans
    donner tout le poids à un pixel isolé. Il peut manquer les obstacles fins.
    La couverture valide doit atteindre 50 %; aucune valeur n'est inventée pour
    une zone non exploitable. Ce calcul n'est pas une garantie anti-collision.
    """
    depth = np.asarray(depth, dtype=np.float32)
    if depth.ndim != 2 or min(depth.shape) < 4:
        raise ValueError("Expected a two-dimensional depth map of at least 4 × 4 pixels")
    if not math.isfinite(max_depth_m) or max_depth_m <= 0:
        raise ValueError("max_depth_m must be finite and positive")
    height, width = depth.shape
    detections = []
    for label, bbox in REGIONS:
        x1, y1, x2, y2 = bbox
        region = depth[
            int(y1 * height) : math.ceil(y2 * height), int(x1 * width) : math.ceil(x2 * width)
        ]
        valid = np.isfinite(region) & (region > 0) & (region <= max_depth_m)
        coverage = float(valid.mean())
        if coverage < 0.5:
            continue
        distance = float(np.percentile(region[valid], 10))
        detections.append(
            DetectionResult(
                label=label,
                confidence=coverage,
                bbox=bbox,
                distance_m=distance,
                distance_source="metric_model",
                observation_type="region",
                depth_valid_fraction=coverage,
            )
        )
    return detections


class DepthAnythingMetricProvider:
    """Charger les poids au démarrage; traiter ensuite les images sur le serveur.

    Les dépendances lourdes sont facultatives pour le mode mock. Le premier
    chargement peut télécharger des poids dans le cache Hugging Face; aucune
    image n'est envoyée à Hugging Face. Une erreur de chargement reste une erreur
    et ne déclenche jamais un remplacement silencieux par des résultats mock.
    Les arguments injectables sont réservés aux tests locaux sans réseau.
    """

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        device: str = "cpu",
        *,
        processor: Any = None,
        model: Any = None,
        torch_module: Any = None,
    ) -> None:
        if (processor is None) != (model is None):
            raise ValueError("Provide both processor and model, or neither")
        if torch_module is None:
            try:
                import torch
            except ImportError as exc:
                raise RuntimeError("Install the vision extra to use depth_anything_metric") from exc
            torch_module = torch
        if model is None:
            try:
                from transformers import AutoConfig, AutoImageProcessor, AutoModelForDepthEstimation
            except ImportError as exc:
                raise RuntimeError("Install the vision extra to use depth_anything_metric") from exc
            config = AutoConfig.from_pretrained(model_id, trust_remote_code=False)
            validate_metric_config(config)
            processor = AutoImageProcessor.from_pretrained(
                model_id,
                trust_remote_code=False,
                use_fast=False,
            )
            model = AutoModelForDepthEstimation.from_pretrained(
                model_id,
                config=config,
                trust_remote_code=False,
                use_safetensors=True,
            )
        self.max_depth_m = validate_metric_config(model.config)
        self.model_id = model_id
        self.model_revision = getattr(model.config, "_commit_hash", None)
        self.device = device
        self._torch = torch_module
        self._processor = processor
        self._model = model.to(device).eval()
        self._lock = threading.Lock()

    def predict_depth(self, image: Image.Image) -> DepthPrediction:
        """Mesurer séparément prétraitement, forward du modèle et extraction métrique.

        Les transferts vers le device appartiennent au prétraitement, le transfert
        vers NumPy au post-traitement. L'attente du verrou n'est pas attribuée au
        forward. La synchronisation CUDA/MPS empêche de mesurer seulement l'envoi
        d'une tâche asynchrone. Aucun poids n'est téléchargé pendant cette méthode.
        """
        with self._lock, self._torch.inference_mode():
            synchronize_device(self._torch, self.device)
            started = perf_counter()
            inputs = self._processor(images=image.convert("RGB"), return_tensors="pt")
            inputs = {name: value.to(self.device) for name, value in inputs.items()}
            synchronize_device(self._torch, self.device)
            preprocess_ms = elapsed_ms(started)
            started = perf_counter()
            outputs = self._model(**inputs)
            synchronize_device(self._torch, self.device)
            inference_ms = elapsed_ms(started)
            started = perf_counter()
            # Read the raw metric tensor, NEVER the pipeline's normalized PIL
            # "depth" visualization. Normalized coordinates work at its native
            # resolution, so interpolation cannot introduce invalid distances.
            raw = outputs.predicted_depth.detach().float().cpu().numpy()
            if raw.ndim != 3 or raw.shape[0] != 1 or min(raw.shape[1:]) < 4:
                raise ValueError("Depth model returned an unexpected batch/shape")
            postprocess_ms = elapsed_ms(started)
        timings = {
            "depth_preprocess_ms": preprocess_ms,
            "depth_inference_ms": inference_ms,
            "depth_postprocess_ms": postprocess_ms,
            "depth_total_ms": preprocess_ms + inference_ms + postprocess_ms,
        }
        return DepthPrediction(
            depth_m=raw[0],
            timing_ms=timings,
            metadata={
                "depth_backend": "depth_anything",
                "depth_semantics": "camera z in meters; not Euclidean range",
                "depth_intrinsics_source": "not estimated by this provider",
                "depth_timing_scope": "depth_inference_ms measures network forward only",
            },
        )

    def analyze(self, image: Image.Image) -> VisionResult:
        started = perf_counter()
        prediction = self.predict_depth(image)
        fusion_started = perf_counter()
        detections = summarize_metric_depth(prediction.depth_m, self.max_depth_m)
        fusion_ms = elapsed_ms(fusion_started)
        if detections:
            closest = min(item.distance_m for item in detections if item.distance_m is not None)
            description = (
                f"Surface la plus proche parmi les zones analysées : environ {closest:.2f} m. "
                "Estimation de profondeur, sans identification de l'objet."
            )
        else:
            description = "Profondeur inexploitable dans les zones analysées; distance inconnue."
        return VisionResult(
            provider="depth_anything_metric",
            detections=detections,
            description=description,
            simulated=False,
            timing_ms={
                **prediction.timing_ms,
                "fusion_ms": fusion_ms,
                "total_vision_ms": elapsed_ms(started),
            },
            metadata={
                "depth_model_id": self.model_id,
                "depth_model_revision": self.model_revision,
                "depth_max_m": self.max_depth_m,
                "device": self.device,
                "depth_percentile": 10,
                "depth_min_valid_fraction": 0.5,
                "confidence_semantics": "valid_depth_pixel_fraction",
                "observation_type": "region",
            },
        )
