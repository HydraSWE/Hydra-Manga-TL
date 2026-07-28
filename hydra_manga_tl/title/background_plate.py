"""Conservative background plates for title cleanup fallback."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np
from PIL import Image

from .mask_extractor import _combined_box, _mask_from_polygons, _normalize_polygons


@dataclass
class TitleBackgroundPlateResult:
    image: Image.Image
    accepted_count: int
    reports: list[dict[str, Any]] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.accepted_count > 0


def _clip_box(box: list[int], size: tuple[int, int], pad: int = 0) -> list[int]:
    return [
        max(0, int(box[0]) - pad),
        max(0, int(box[1]) - pad),
        min(size[0], int(box[2]) + pad),
        min(size[1], int(box[3]) + pad),
    ]


def _gradient_plate(crop: np.ndarray, ring: np.ndarray) -> np.ndarray:
    height, width = crop.shape[:2]
    ys, xs = np.where(ring > 0)
    if len(xs) < 8:
        color = np.median(crop.reshape(-1, 3), axis=0)
        return np.tile(color.astype(np.uint8), (height, width, 1))

    left = crop[ys[xs <= np.median(xs)], xs[xs <= np.median(xs)]]
    right = crop[ys[xs > np.median(xs)], xs[xs > np.median(xs)]]
    top = crop[ys[ys <= np.median(ys)], xs[ys <= np.median(ys)]]
    bottom = crop[ys[ys > np.median(ys)], xs[ys > np.median(ys)]]

    left_color = np.median(left, axis=0) if len(left) else np.median(crop[ring > 0], axis=0)
    right_color = np.median(right, axis=0) if len(right) else left_color
    top_color = np.median(top, axis=0) if len(top) else left_color
    bottom_color = np.median(bottom, axis=0) if len(bottom) else top_color

    horizontal_delta = float(np.linalg.norm(left_color.astype(float) - right_color.astype(float)))
    vertical_delta = float(np.linalg.norm(top_color.astype(float) - bottom_color.astype(float)))
    if horizontal_delta >= vertical_delta:
        t = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :, None]
        row = left_color[None, None, :] * (1.0 - t) + right_color[None, None, :] * t
        return np.repeat(row, height, axis=0).astype(np.uint8)
    t = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None, None]
    column = top_color[None, None, :] * (1.0 - t) + bottom_color[None, None, :] * t
    return np.repeat(column, width, axis=1).astype(np.uint8)


def _plate_for_group(image: Image.Image, group: dict[str, Any], image_size: tuple[int, int]) -> tuple[np.ndarray | None, dict[str, Any]]:
    polygons = _normalize_polygons(group.get("source_polygons") or [group.get("polygon", [])])
    box = _combined_box(polygons, image_size, pad=4)
    if box is None:
        return None, {
            "title_background_plate_method": "skipped",
            "title_background_plate_warning": "missing_title_geometry",
            "title_background_plate_confidence": 0.0,
        }

    source_box = _combined_box(polygons, image_size, pad=0) or box
    if len(polygons) <= 1:
        source_area = max(1, (source_box[2] - source_box[0]) * (source_box[3] - source_box[1]))
        image_area = max(1, image_size[0] * image_size[1])
        if source_area / image_area > 0.28:
            return None, {
                "title_background_plate_method": "skipped",
                "title_background_plate_warning": "title_region_too_large_for_plate",
                "title_background_plate_confidence": 0.0,
            }

    padded_box = _clip_box(box, image_size, pad=24)
    x1, y1, x2, y2 = padded_box
    crop = np.asarray(image.crop((x1, y1, x2, y2)).convert("RGB"))
    local_polygons = [[[x - x1, y - y1] for x, y in polygon] for polygon in polygons]
    plate_mask = _mask_from_polygons((x2 - x1, y2 - y1), local_polygons, dilation=6)
    if not np.count_nonzero(plate_mask):
        return None, {
            "title_background_plate_method": "skipped",
            "title_background_plate_warning": "empty_plate_mask",
            "title_background_plate_confidence": 0.0,
        }

    ring_outer = cv2.dilate(plate_mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (41, 41)))
    ring_inner = cv2.dilate(plate_mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 13)))
    ring = cv2.subtract(ring_outer, ring_inner)
    ring_pixels = crop[ring > 0]
    if len(ring_pixels) < max(40, int(np.count_nonzero(plate_mask) * 0.08)):
        return None, {
            "title_background_plate_method": "skipped",
            "title_background_plate_warning": "insufficient_background_sample",
            "title_background_plate_confidence": 0.0,
        }

    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 55, 150)
    ring_edge_density = float(np.count_nonzero(edges[ring > 0]) / max(1, np.count_nonzero(ring)))
    color_std = float(np.mean(np.std(ring_pixels.astype(np.float32), axis=0)))
    if color_std > 58.0 or ring_edge_density > 0.19:
        warning = "complex_background_rejected"
        if ring_edge_density > 0.19:
            warning = "line_art_overlap_rejected"
        return None, {
            "title_background_plate_method": "skipped",
            "title_background_plate_warning": warning,
            "title_background_plate_confidence": 0.0,
            "title_background_plate_color_std": round(color_std, 2),
            "title_background_plate_edge_density": round(ring_edge_density, 4),
        }

    plate = _gradient_plate(crop, ring)
    feather = cv2.GaussianBlur(plate_mask, (0, 0), sigmaX=2.2, sigmaY=2.2).astype(np.float32) / 255.0
    feather = feather[:, :, None]
    blended = (crop.astype(np.float32) * (1.0 - feather) + plate.astype(np.float32) * feather).astype(np.uint8)
    full = np.asarray(image.convert("RGB")).copy()
    full[y1:y2, x1:x2] = blended
    confidence = max(0.55, min(0.92, 0.9 - color_std / 180.0 - ring_edge_density * 0.7))
    return full, {
        "title_background_plate_method": "local-gradient-plate",
        "title_background_plate_warning": "",
        "title_background_plate_confidence": round(confidence, 3),
        "title_background_plate_color_std": round(color_std, 2),
        "title_background_plate_edge_density": round(ring_edge_density, 4),
        "title_background_plate_box": [x1, y1, x2, y2],
    }


def apply_title_background_plates(image: Image.Image, groups: list[dict[str, Any]], image_size: tuple[int, int] | None = None) -> TitleBackgroundPlateResult:
    size = image_size or image.size
    current = image.convert("RGB")
    accepted = 0
    reports: list[dict[str, Any]] = []
    for group in groups:
        array, report = _plate_for_group(current, group, size)
        report["group"] = group.get("index")
        group["title_background_plate_report"] = report
        reports.append(report)
        if array is None:
            continue
        current = Image.fromarray(array).convert("RGB")
        accepted += 1
    return TitleBackgroundPlateResult(current, accepted, reports)
