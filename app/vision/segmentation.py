"""Fuse instance masks with raw metric depth, without sampling the background."""

import math

import numpy as np
from PIL import Image

from app.vision.base import DetectionResult
from app.vision.urban import COCO_LABEL_ALIASES, normalize_object_bbox


def mask_depth(mask, depth_m, max_depth_m):
    depth = np.asarray(depth_m, dtype=np.float32)
    mask = np.asarray(mask, dtype=bool)
    if mask.ndim != 2 or depth.ndim != 2:
        raise ValueError("Expected two-dimensional mask and depth")
    if mask.shape != depth.shape:
        mask = np.asarray(
            Image.fromarray(mask).resize(depth.shape[::-1], Image.Resampling.NEAREST), dtype=bool
        )
    pixels = depth[mask]
    valid = np.isfinite(pixels) & (pixels > 0) & (pixels <= max_depth_m)
    fraction = float(valid.mean()) if pixels.size else 0.0
    if valid.sum() < 16 or fraction < 0.5:
        return None, fraction
    return float(np.percentile(pixels[valid], 20)), fraction


def mask_geometry(mask):
    """Simplified display contours; distance always uses the complete binary mask."""
    import cv2

    mask = np.asarray(mask, dtype=np.uint8)
    height, width = mask.shape
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    polygons = []
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:8]:
        epsilon = max(1.0, cv2.arcLength(contour, True) * 0.003)
        points = cv2.approxPolyDP(contour, epsilon, True).reshape(-1, 2)
        while len(points) > 64:
            epsilon *= 1.5
            points = cv2.approxPolyDP(contour, epsilon, True).reshape(-1, 2)
        if len(points) >= 3:
            polygons.append(
                [[round(float(x / width), 5), round(float(y / height), 5)] for x, y in points]
            )
    ys, xs = np.nonzero(mask)
    edges = []
    if len(xs):
        if xs.min() < width * 0.02:
            edges.append("left")
        if xs.max() >= width * 0.98:
            edges.append("right")
        if ys.min() < height * 0.02:
            edges.append("top")
        if ys.max() >= height * 0.98:
            edges.append("bottom")
    return {"polygons": polygons, "mask_area_ratio": float(mask.mean()), "mask_edges": edges}


# Same-class suppression requires overlapping instance masks as well as boxes.
# Nearby people with disjoint silhouettes must remain separate observations.
DUPLICATE_BOX_IOU = 0.60
DUPLICATE_MASK_IOU = 0.55


def unique_instances(observations, masks):
    kept = []
    areas = [int(mask.sum()) for mask in masks]
    for i in sorted(
        range(len(observations)), key=lambda j: observations[j].confidence, reverse=True
    ):
        item = observations[i]
        duplicate = False
        for j in kept:
            other = observations[j]
            if item.label != other.label or not areas[i] or not areas[j]:
                continue
            a, b = item.bbox, other.bbox
            intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
                0, min(a[3], b[3]) - max(a[1], b[1])
            )
            union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
            if intersection / union < DUPLICATE_BOX_IOU:
                continue
            overlap = int(np.count_nonzero(masks[i] & masks[j]))
            if overlap / (areas[i] + areas[j] - overlap) >= DUPLICATE_MASK_IOU:
                duplicate = True
                break
        if not duplicate:
            kept.append(i)
    return [observations[i] for i in kept], [masks[i] for i in kept]


def fuse_segment_depth(*, masks, geometry=mask_geometry, return_masks=False, **kwargs):
    boxes, scores, labels = (np.asarray(kwargs[k]) for k in ("boxes", "scores", "labels"))
    if (
        boxes.ndim != 2
        or boxes.shape[1:] != (4,)
        or scores.shape != (len(boxes),)
        or labels.shape != (len(boxes),)
    ):
        raise ValueError("Invalid detection tensor shapes")
    if masks is not None and len(masks) != len(boxes):
        raise ValueError("Instance masks and detections are not aligned")
    observations, accepted_masks = [], []
    for index, (box, score, label_id) in enumerate(zip(boxes, scores, labels, strict=True)):
        bbox = normalize_object_bbox(box, kwargs["image_size"])
        if (
            bbox is None
            or not math.isfinite(score)
            or not kwargs.get("confidence_threshold", 0.5) <= score <= 1
            or not np.isfinite(label_id)
            or int(label_id) != label_id
        ):
            continue
        label = kwargs["id2label"].get(int(label_id), kwargs["id2label"].get(str(int(label_id))))
        if (
            not isinstance(label, str)
            or not label.strip()
            or label.lower() in {"n/a", "background"}
        ):
            continue
        label = COCO_LABEL_ALIASES.get(label, label)
        shape = (kwargs["image_size"][1], kwargs["image_size"][0])
        mask = np.asarray(masks[index]) > 0.5 if masks is not None else np.zeros(shape, dtype=bool)
        if mask.shape != shape:
            raise ValueError("retina_masks must match the original RGB dimensions")
        distance, fraction = (None, None)
        if kwargs.get("depth_m") is not None:
            distance, fraction = mask_depth(mask, kwargs["depth_m"], kwargs["max_depth_m"])
        observations.append(
            DetectionResult(
                label,
                float(score),
                bbox,
                distance,
                "metric_model",
                "object",
                fraction,
                geometry(mask),
            )
        )
        accepted_masks.append(mask)
    observations, accepted_masks = unique_instances(observations, accepted_masks)
    return (observations, accepted_masks) if return_masks else observations
