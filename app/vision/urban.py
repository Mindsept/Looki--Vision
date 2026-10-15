"""Objets COCO et profondeur métrique extérieure, entièrement sur le serveur.

RT-DETR localise les objets; le provider métrique fournit les mètres estimés. Le
percentile de profondeur dans la partie centrale d'une boîte réduit le mélange
avec le fond sans constituer une segmentation de l'objet. Les occlusions,
objets fins, surfaces transparentes et variations de caméra restent des limites.

Sources primaires : https://huggingface.co/PekingU/rtdetr_r18vd et
https://huggingface.co/depth-anything/Depth-Anything-V2-Metric-Outdoor-Small-hf
"""

import math
import threading
from collections.abc import Mapping
from time import perf_counter
from typing import Any

import numpy as np
from PIL import Image

from app.vision.base import DetectionResult, VisionResult
from app.vision.depth_anything import DEFAULT_OUTDOOR_MODEL_ID, DepthAnythingMetricProvider
from app.vision.timing import elapsed_ms, synchronize_device

DEFAULT_OBJECT_MODEL_ID = "PekingU/rtdetr_r18vd"
DEPTH_PERCENTILE = 20
CENTRAL_BBOX_RATIO = 0.8
MIN_DEPTH_VALID_FRACTION = 0.5

# The official checkpoint uses several historical COCO/VOC spelling aliases.
# Store canonical English COCO labels; presentation/translation belongs outside
# the inference layer, and no language model is involved.
COCO_LABEL_ALIASES = {
    "motorbike": "motorcycle",
    "aeroplane": "airplane",
    "sofa": "couch",
    "pottedplant": "potted plant",
    "diningtable": "dining table",
    "tvmonitor": "tv",
}


def normalize_object_bbox(
    box: np.ndarray,
    image_size: tuple[int, int],
) -> tuple[float, float, float, float] | None:
    """Passer de pixels xyxy à [0,1]; ignorer les boîtes invalides ou hors champ."""
    width, height = image_size
    if width <= 0 or height <= 0:
        raise ValueError("Image dimensions must be positive")
    box = np.asarray(box, dtype=np.float64)
    if box.shape != (4,) or not np.isfinite(box).all():
        return None
    x1, y1, x2, y2 = np.clip(box / [width, height, width, height], 0.0, 1.0)
    if x1 >= x2 or y1 >= y2:
        return None
    return float(x1), float(y1), float(x2), float(y2)


def object_depth(
    depth_m: np.ndarray,
    bbox: tuple[float, float, float, float],
    max_depth_m: float,
) -> tuple[float | None, float]:
    """p20 sur les 80 % centraux de la boîte; garder inconnue toute zone peu valide.

    La profondeur reste à sa résolution native, sans conversion en image 8 bits.
    La boîte normalisée relie les deux résolutions par leurs coordonnées image.
    La fraction retournée mesure uniquement la couverture de données valides.
    """
    depth = np.asarray(depth_m, dtype=np.float32)
    if depth.ndim != 2 or min(depth.shape) < 4:
        raise ValueError("Expected a two-dimensional depth map of at least 4 × 4 pixels")
    if not math.isfinite(max_depth_m) or max_depth_m <= 0:
        raise ValueError("max_depth_m must be finite and positive")
    height, width = depth.shape
    x1, y1, x2, y2 = bbox
    margin_x = (x2 - x1) * (1 - CENTRAL_BBOX_RATIO) / 2
    margin_y = (y2 - y1) * (1 - CENTRAL_BBOX_RATIO) / 2
    region = depth[
        math.floor((y1 + margin_y) * height + 1e-9) : math.ceil((y2 - margin_y) * height - 1e-9),
        math.floor((x1 + margin_x) * width + 1e-9) : math.ceil((x2 - margin_x) * width - 1e-9),
    ]
    if not region.size:
        return None, 0.0
    valid = np.isfinite(region) & (region > 0) & (region <= max_depth_m)
    fraction = float(valid.mean())
    if fraction < MIN_DEPTH_VALID_FRACTION:
        return None, fraction
    return float(np.percentile(region[valid], DEPTH_PERCENTILE)), fraction


def fuse_object_depth(
    *,
    boxes: np.ndarray,
    scores: np.ndarray,
    labels: np.ndarray,
    image_size: tuple[int, int],
    depth_m: np.ndarray,
    max_depth_m: float,
    id2label: Mapping[int | str, str],
    confidence_threshold: float = 0.5,
) -> list[DetectionResult]:
    """Associer chaque vraie détection à la profondeur, sans créer d'objets ROI.

    Une boîte/score invalide est ignoré. Une boîte valide sans profondeur
    exploitable reste un objet détecté avec ``distance_m=None``. Un label absent
    de la configuration n'est pas transformé en une classe sémantique inventée.
    """
    if not math.isfinite(confidence_threshold) or not 0 <= confidence_threshold <= 1:
        raise ValueError("Object confidence threshold must be between 0 and 1")
    boxes = np.asarray(boxes, dtype=np.float64)
    scores = np.asarray(scores, dtype=np.float64)
    labels = np.asarray(labels)
    if boxes.ndim != 2 or boxes.shape[1:] != (4,) or scores.ndim != 1 or labels.ndim != 1:
        raise ValueError("Unexpected object detection tensor shapes")
    if not len(boxes) == len(scores) == len(labels):
        raise ValueError("Object boxes, scores and labels must have matching lengths")
    observations = []
    for box, score, label_id in zip(boxes, scores, labels, strict=True):
        if not math.isfinite(score) or not confidence_threshold <= score <= 1:
            continue
        bbox = normalize_object_bbox(box, image_size)
        if bbox is None or not np.isfinite(label_id) or int(label_id) != label_id:
            continue
        label = id2label.get(int(label_id), id2label.get(str(int(label_id))))
        if (
            not isinstance(label, str)
            or not label.strip()
            or label.lower() in {"n/a", "background"}
        ):
            continue
        label = COCO_LABEL_ALIASES.get(label, label)
        distance, fraction = object_depth(depth_m, bbox, max_depth_m)
        observations.append(
            DetectionResult(
                label=label,
                confidence=float(score),
                bbox=bbox,
                distance_m=distance,
                distance_source="metric_model",
                observation_type="object",
                depth_valid_fraction=fraction,
            )
        )
    return observations


class UrbanObjectsProvider:
    """RT-DETR + carte de profondeur extérieure; chargement explicite au démarrage."""

    def __init__(
        self,
        depth_model_id: str = DEFAULT_OUTDOOR_MODEL_ID,
        object_model_id: str = DEFAULT_OBJECT_MODEL_ID,
        device: str = "cpu",
        object_confidence_threshold: float = 0.5,
        *,
        depth_provider: Any = None,
        detector_processor: Any = None,
        detector_model: Any = None,
        torch_module: Any = None,
    ) -> None:
        if (
            not math.isfinite(object_confidence_threshold)
            or not 0 <= object_confidence_threshold <= 1
        ):
            raise ValueError("Object confidence threshold must be between 0 and 1")
        if (detector_processor is None) != (detector_model is None):
            raise ValueError("Provide both detector_processor and detector_model, or neither")
        if torch_module is None:
            try:
                import torch
            except ImportError as exc:
                raise RuntimeError("Install the vision extra to use urban_objects") from exc
            torch_module = torch
        if detector_model is None:
            try:
                from transformers import AutoImageProcessor, AutoModelForObjectDetection
            except ImportError as exc:
                raise RuntimeError("Install the vision extra to use urban_objects") from exc
            detector_processor = AutoImageProcessor.from_pretrained(
                object_model_id,
                trust_remote_code=False,
                use_fast=False,
            )
            detector_model = AutoModelForObjectDetection.from_pretrained(
                object_model_id,
                trust_remote_code=False,
                use_safetensors=True,
            )
        if getattr(detector_model.config, "model_type", None) != "rt_detr":
            raise ValueError("urban_objects requires an RT-DETR object detection model")
        if not getattr(detector_model.config, "id2label", None):
            raise ValueError("The object model must declare its id2label mapping")
        self._depth = depth_provider or DepthAnythingMetricProvider(
            model_id=depth_model_id,
            device=device,
            torch_module=torch_module,
        )
        self.depth_model_id = depth_model_id
        self.object_model_id = object_model_id
        self.device = device
        self.object_confidence_threshold = object_confidence_threshold
        self._torch = torch_module
        self._processor = detector_processor
        self._model = detector_model.to(device).eval()
        self._lock = threading.Lock()

    def analyze(self, image: Image.Image) -> VisionResult:
        with self._lock:
            started = perf_counter()
            image = image.convert("RGB")
            depth = self._depth.predict_depth(image)
            with self._torch.inference_mode():
                synchronize_device(self._torch, self.device)
                step = perf_counter()
                inputs = self._processor(images=image, return_tensors="pt")
                inputs = {name: value.to(self.device) for name, value in inputs.items()}
                synchronize_device(self._torch, self.device)
                preprocess_ms = elapsed_ms(step)
                step = perf_counter()
                outputs = self._model(**inputs)
                synchronize_device(self._torch, self.device)
                inference_ms = elapsed_ms(step)
                step = perf_counter()
                results = self._processor.post_process_object_detection(
                    outputs,
                    target_sizes=[(image.height, image.width)],
                    threshold=0.0,
                )
                if len(results) != 1:
                    raise ValueError("Object model returned an unexpected batch")
                detected = results[0]
                boxes = detected["boxes"].detach().float().cpu().numpy()
                scores = detected["scores"].detach().float().cpu().numpy()
                labels = detected["labels"].detach().cpu().numpy()
                postprocess_ms = elapsed_ms(step)
            step = perf_counter()
            observations = fuse_object_depth(
                boxes=boxes,
                scores=scores,
                labels=labels,
                image_size=image.size,
                depth_m=depth.depth_m,
                max_depth_m=self._depth.max_depth_m,
                id2label=self._model.config.id2label,
                confidence_threshold=self.object_confidence_threshold,
            )
            fusion_ms = elapsed_ms(step)
            description = (
                "Objets détectés : "
                + ", ".join(sorted({item.label for item in observations}))
                + "."
                if observations
                else "Aucun objet COCO détecté au seuil choisi; cela ne prouve pas l'absence d'obstacle."
            )
            return VisionResult(
                provider="urban_objects",
                detections=observations,
                description=description,
                simulated=False,
                timing_ms={
                    **depth.timing_ms,
                    "detection_preprocess_ms": preprocess_ms,
                    "detection_inference_ms": inference_ms,
                    "detection_postprocess_ms": postprocess_ms,
                    "detection_total_ms": preprocess_ms + inference_ms + postprocess_ms,
                    "fusion_ms": fusion_ms,
                    "total_vision_ms": elapsed_ms(started),
                },
                metadata={
                    **depth.metadata,
                    "depth_model_id": self.depth_model_id,
                    "depth_model_revision": getattr(self._depth, "model_revision", None),
                    "depth_max_m": self._depth.max_depth_m,
                    "object_model_id": self.object_model_id,
                    "object_model_revision": getattr(self._model.config, "_commit_hash", None),
                    "device": self.device,
                    "observation_type": "object",
                    "label_space": "COCO English; checkpoint spelling aliases normalized",
                    "object_confidence_threshold": self.object_confidence_threshold,
                    "confidence_semantics": "object_detection_score",
                    "depth_valid_fraction_semantics": "valid_depth_pixel_fraction",
                    "depth_percentile": DEPTH_PERCENTILE,
                    "central_bbox_ratio": CENTRAL_BBOX_RATIO,
                    "depth_min_valid_fraction": MIN_DEPTH_VALID_FRACTION,
                    "depth_map_width": int(depth.depth_m.shape[1]),
                    "depth_map_height": int(depth.depth_m.shape[0]),
                    "depth_method": "Raw metric depth p20 in central 80% bbox; no object segmentation",
                    "timing_method": "perf_counter wall time; CUDA/MPS synchronized; model loading excluded",
                },
            )
