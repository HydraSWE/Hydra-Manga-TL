"""Bubble and text-region segmentation primitives for the v0.8 pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from hydra_manga_tl.phase.renderer import compute_safe_text_area, expanded_box


@dataclass(frozen=True)
class BubbleSegmentation:
    kind: str
    bbox: list[int]
    safe_area: list[int]
    mask_path: str
    confidence: float
    method: str
    mask_quality: dict[str, Any]
    failure_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def bbox_from_polygon(polygon: list[list[int]]) -> list[int]:
    xs = [int(point[0]) for point in polygon]
    ys = [int(point[1]) for point in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def _clip_box(box: list[int], size: tuple[int, int]) -> list[int]:
    return [
        max(0, int(box[0])),
        max(0, int(box[1])),
        min(size[0], int(box[2])),
        min(size[1], int(box[3])),
    ]


def _rect_mask(size: tuple[int, int], box: list[int]) -> np.ndarray:
    width, height = size
    mask = np.zeros((height, width), dtype=np.uint8)
    x1, y1, x2, y2 = _clip_box(box, size)
    if x2 > x1 and y2 > y1:
        mask[y1:y2, x1:x2] = 255
    return mask


def _component_mask(image: Image.Image, polygon: list[list[int]]) -> tuple[np.ndarray, list[int], float, dict[str, Any]] | None:
    gray = np.asarray(image.convert("L"))
    height, width = gray.shape
    x1, y1, x2, y2 = bbox_from_polygon(polygon)
    source_area = max(1, (x2 - x1) * (y2 - y1))
    source_cx, source_cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    binary = cv2.adaptiveThreshold(blurred, 1, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 41, 10)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)

    best: tuple[float, int] | None = None
    best_quality: dict[str, Any] = {}
    for label in range(1, count):
        bx, by, bw, bh, area = [int(value) for value in stats[label]]
        if area < source_area * 1.05 or area > width * height * 0.55:
            continue
        ix1, iy1 = max(bx, x1), max(by, y1)
        ix2, iy2 = min(bx + bw, x2), min(by + bh, y2)
        coverage = (max(0, ix2 - ix1) * max(0, iy2 - iy1)) / source_area
        if coverage < 0.65:
            continue
        cx, cy = centroids[label]
        distance = ((cx - source_cx) ** 2 + (cy - source_cy) ** 2) / max(1, source_area)
        area_ratio = area / max(1, source_area)
        score = coverage * 5.0 - min(3.0, distance) - min(2.0, area / max(1, source_area) * 0.08)
        if best is None or score > best[0]:
            best = (score, label)
            best_quality = {
                "coverage": round(float(coverage), 4),
                "centroid_distance": round(float(distance), 4),
                "area_ratio": round(float(area_ratio), 4),
                "page_area_ratio": round(float(area / max(1, width * height)), 4),
            }
    if best is None:
        return None

    label = best[1]
    mask = np.uint8(labels == label) * 255
    bx, by, bw, bh, area = [int(value) for value in stats[label]]
    return mask, [bx, by, bx + bw, by + bh], min(0.99, max(0.35, best[0] / 5.0)), best_quality


def _component_failure_reason(confidence: float, quality: dict[str, Any]) -> str:
    page_ratio = float(quality.get("page_area_ratio", 0.0) or 0.0)
    area_ratio = float(quality.get("area_ratio", 0.0) or 0.0)
    if page_ratio > 0.45:
        return "page_size_rejection"
    if area_ratio > 55.0:
        return "area_ratio_rejection"
    if confidence < 0.62:
        return "low_mask_confidence"
    return ""


def segment_bubble(
    image: Image.Image,
    polygon: list[list[int]],
    *,
    bubble_type: str = "speech",
    padding: int = 5,
    mask_path: Path,
) -> BubbleSegmentation:
    """Segment a bubble/text area and save its mask as an 8-bit PNG."""
    mask_path.parent.mkdir(parents=True, exist_ok=True)
    kind = bubble_type or "speech"
    page_size = image.size
    method = "rectangular_fallback"
    confidence = 0.45
    mask_quality: dict[str, Any] = {}
    failure_reason = ""

    if kind in {"dialogue", "speech", "thought", "narration"}:
        component = _component_mask(image, polygon)
    else:
        component = None

    if component is not None:
        candidate_mask, candidate_bbox, candidate_confidence, candidate_quality = component
        rejected_reason = _component_failure_reason(candidate_confidence, candidate_quality)
        if not rejected_reason:
            mask, bbox, confidence, mask_quality = candidate_mask, candidate_bbox, candidate_confidence, candidate_quality
            method = "light_component"
        else:
            component = None
            failure_reason = rejected_reason

    if component is None:
        bbox = expanded_box(polygon, page_size)
        mask = _rect_mask(page_size, bbox)
        if not failure_reason:
            failure_reason = "low_mask_confidence"
        x1, y1, x2, y2 = bbox
        mask_quality = {
            "coverage": 0.0,
            "centroid_distance": 0.0,
            "area_ratio": round(((x2 - x1) * (y2 - y1)) / max(1, (bbox_from_polygon(polygon)[2] - bbox_from_polygon(polygon)[0]) * (bbox_from_polygon(polygon)[3] - bbox_from_polygon(polygon)[1])), 4),
            "page_area_ratio": round(((x2 - x1) * (y2 - y1)) / max(1, page_size[0] * page_size[1]), 4),
        }

    safe_area = compute_safe_text_area(image, polygon, padding) if method == "light_component" else None
    if safe_area is None:
        x1, y1, x2, y2 = _clip_box(bbox, page_size)
        inset = max(int(padding), int(min(max(1, x2 - x1), max(1, y2 - y1)) * 0.08))
        safe_area = _clip_box([x1 + inset, y1 + inset, x2 - inset, y2 - inset], page_size)
        if safe_area[2] <= safe_area[0] or safe_area[3] <= safe_area[1]:
            safe_area = [x1, y1, x2, y2]

    Image.fromarray(mask, mode="L").save(mask_path)
    return BubbleSegmentation(
        kind=kind,
        bbox=[int(value) for value in bbox],
        safe_area=[int(value) for value in safe_area],
        mask_path=str(mask_path.resolve()),
        confidence=float(confidence),
        method=method,
        mask_quality=mask_quality,
        failure_reason=failure_reason,
    )


def reusable_segmentation(payload: dict[str, Any] | None, image_size: tuple[int, int]) -> dict[str, Any] | None:
    """Validate a stored segmentation before render-time reuse."""
    if not isinstance(payload, dict) or payload.get("failure_reason"):
        return None
    if float(payload.get("confidence", 0.0) or 0.0) < 0.60:
        return None
    safe_area = payload.get("safe_area")
    bbox = payload.get("bbox")
    if not isinstance(safe_area, list) or len(safe_area) != 4:
        return None
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    clipped_safe = _clip_box([int(value) for value in safe_area], image_size)
    clipped_bbox = _clip_box([int(value) for value in bbox], image_size)
    if clipped_safe[2] <= clipped_safe[0] or clipped_safe[3] <= clipped_safe[1]:
        return None
    return {**payload, "safe_area": clipped_safe, "bbox": clipped_bbox}
