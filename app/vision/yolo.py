"""YOLO26 Nano + metric depth on the exact same RGB image."""

import threading
from time import perf_counter

import numpy as np
from PIL import Image

from app.vision.base import VisionResult
from app.vision.depth_anything import DepthAnythingMetricProvider
from app.vision.timing import elapsed_ms
from app.vision.urban import (
    CENTRAL_BBOX_RATIO,
    DEPTH_PERCENTILE,
    MIN_DEPTH_VALID_FRACTION,
    fuse_object_depth,
)

YOLO_REPOSITORY = "Ultralytics/YOLO26"
YOLO_REVISION = "070ac3c51435984d992ba92eb3f234daf4e4500d"
YOLO_FILENAME = "yolo26n.pt"


class YoloObjectsProvider:
    def __init__(
        self,
        depth_model_id,
        device="cpu",
        object_confidence_threshold=0.5,
        depth_provider=None,
        image_size=640,
        cpu_threads=4,
        *,
        detector=None,
        segment=False,
    ):
        if detector is None:
            import torch
            from huggingface_hub import hf_hub_download
            from ultralytics import YOLO

            torch.set_num_threads(cpu_threads)
            # Only the pinned official checkpoint, never a user-uploaded model.
            filename = "yolo26n-seg.pt" if segment else YOLO_FILENAME
            weights = hf_hub_download(YOLO_REPOSITORY, filename, revision=YOLO_REVISION)
            detector = YOLO(weights, task="segment" if segment else "detect")
            # Ultralytics selects its own CPU thread count during predictor setup.
            # Reapply this deployment's budget after setup, before actual inference.
            detector.add_callback("on_predict_start", lambda _: torch.set_num_threads(cpu_threads))
        self.segment = segment
        self.detector = detector
        self.depth = depth_provider or DepthAnythingMetricProvider(depth_model_id, device)
        self.depth_model_id = depth_model_id
        self.device = device
        self.confidence = object_confidence_threshold
        self.image_size = image_size
        self.cpu_threads = cpu_threads
        self._lock = threading.Lock()

    def analyze(self, image: Image.Image) -> VisionResult:
        return self._analyze(image, visual_only=False, include_masks=False)

    def analyze_visual(self, image: Image.Image, *, include_masks=False) -> VisionResult:
        if not self.segment:
            raise ValueError("Independent depth requires segmentation")
        return self._analyze(image, visual_only=True, include_masks=include_masks)

    def _analyze(self, image, *, visual_only, include_masks):
        with self._lock:
            started = perf_counter()
            image = image.convert("RGB")
            detection_started = perf_counter()
            results = self.detector.predict(
                source=image,
                imgsz=self.image_size,
                conf=self.confidence,
                device=self.device,
                verbose=False,
                save=False,
                max_det=100,
                **({"retina_masks": True} if self.segment else {}),
            )
            detection_total = elapsed_ms(detection_started)
            if len(results) != 1:
                raise ValueError("Expected one YOLO result")
            result = results[0]
            depth = None if visual_only else self.depth.predict_depth(image)
            fusion_started = perf_counter()
            fusion = fuse_object_depth
            extra = {}
            if self.segment:
                from app.vision.segmentation import fuse_segment_depth

                fusion = fuse_segment_depth
                extra["return_masks"] = include_masks
                extra["masks"] = (
                    result.masks.data.cpu().numpy() if result.masks is not None else None
                )
            detections = fusion(
                **extra,
                boxes=np.asarray(result.boxes.xyxy.cpu(), dtype=np.float32),
                scores=np.asarray(result.boxes.conf.cpu(), dtype=np.float32),
                labels=np.asarray(result.boxes.cls.cpu(), dtype=np.float32),
                image_size=image.size,
                depth_m=depth.depth_m if depth else None,
                max_depth_m=self.depth.max_depth_m,
                id2label=result.names,
                confidence_threshold=self.confidence,
            )
            packed_masks = None
            if include_masks:
                from app.vision.masks import pack_masks

                detections, masks = detections
                if detections:
                    packed_masks = pack_masks(masks, image.size)
            fusion_ms = elapsed_ms(fusion_started)
            return VisionResult(
                depth_masks=packed_masks,
                provider="yolo_seg_track" if self.segment else "yolo_objects",
                detections=detections,
                description=(
                    "Objets détectés : " + ", ".join(sorted({d.label for d in detections})) + "."
                    if detections
                    else "Aucun objet détecté au seuil choisi. Cela ne prouve pas l'absence d'obstacle."
                ),
                simulated=False,
                timing_ms={
                    **(depth.timing_ms if depth else {}),
                    "detection_preprocess_ms": float(result.speed["preprocess"]),
                    "detection_inference_ms": float(result.speed["inference"]),
                    "detection_postprocess_ms": float(result.speed["postprocess"]),
                    "detection_total_ms": detection_total,
                    "fusion_ms": fusion_ms,
                    "total_vision_ms": elapsed_ms(started),
                },
                metadata={
                    **(depth.metadata if depth else {}),
                    "depth_mode": "independent" if visual_only else "synchronous",
                    "object_model_id": f"{YOLO_REPOSITORY}/{'yolo26n-seg.pt' if self.segment else YOLO_FILENAME}",
                    "object_model_revision": YOLO_REVISION,
                    "object_input_size": self.image_size,
                    "duplicate_suppression": "same_class_box_iou_0.60_and_mask_iou_0.55"
                    if self.segment
                    else None,
                    "object_confidence_threshold": self.confidence,
                    "depth_model_id": self.depth_model_id,
                    "depth_model_revision": getattr(self.depth, "model_revision", None),
                    "depth_max_m": self.depth.max_depth_m,
                    "device": self.device,
                    "cpu_threads": self.cpu_threads,
                    "observation_type": "object",
                    "confidence_semantics": "object_detection_score",
                    "depth_valid_fraction_semantics": "valid_depth_pixel_fraction",
                    "depth_percentile": DEPTH_PERCENTILE,
                    "central_bbox_ratio": None if self.segment else CENTRAL_BBOX_RATIO,
                    "depth_min_valid_fraction": MIN_DEPTH_VALID_FRACTION,
                    "depth_method": (
                        "Raw metric depth p20 inside full instance mask; no bbox fallback"
                        if self.segment
                        else "Raw metric depth p20 in central 80% bbox; same image; no temporal reuse"
                    ),
                },
            )
