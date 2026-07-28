"""Utility helpers for HSTR geometry, masks, colors, and style cache."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from hydra_manga_tl.core.paths import PATHS

from .style_profile import TitleStyleProfile


def box_from_polygon(polygon: list[list[int]]) -> list[int]:
    xs = [int(point[0]) for point in polygon]
    ys = [int(point[1]) for point in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def clip_box(box: list[int], size: tuple[int, int]) -> list[int]:
    return [
        max(0, int(box[0])),
        max(0, int(box[1])),
        min(size[0], int(box[2])),
        min(size[1], int(box[3])),
    ]


def polygon_mask(size: tuple[int, int], polygons: list[list[list[int]]] | list[list[int]], dilation: int = 0) -> Image.Image:
    width, height = size
    mask = np.zeros((height, width), dtype=np.uint8)
    if polygons and polygons and isinstance(polygons[0][0], (int, float)):  # type: ignore[index]
        polygons = [polygons]  # type: ignore[assignment]
    for polygon in polygons:  # type: ignore[assignment]
        if not polygon:
            continue
        points = np.asarray(polygon, dtype=np.int32)
        cv2.fillPoly(mask, [points], 255)
    if dilation > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilation * 2 + 1, dilation * 2 + 1))
        mask = cv2.dilate(mask, kernel)
    return Image.fromarray(mask, mode="L")


def masked_pixels(image: Image.Image, mask: Image.Image, box: list[int]) -> np.ndarray:
    clipped = clip_box(box, image.size)
    if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
        return np.empty((0, 3), dtype=np.uint8)
    crop = np.asarray(image.crop(tuple(clipped)).convert("RGB"))
    mask_crop = np.asarray(mask.crop(tuple(clipped)).convert("L")) > 0
    if mask_crop.shape[:2] != crop.shape[:2]:
        return crop.reshape(-1, 3)
    return crop[mask_crop] if mask_crop.any() else crop.reshape(-1, 3)


def dominant_color(pixels: np.ndarray) -> tuple[int, int, int] | None:
    if pixels.size == 0:
        return None
    quantized = (pixels.astype(np.uint16) // 24) * 24
    colors, counts = np.unique(quantized.reshape(-1, 3), axis=0, return_counts=True)
    return tuple(int(value) for value in colors[int(np.argmax(counts))])


def average_color(pixels: np.ndarray) -> tuple[int, int, int] | None:
    if pixels.size == 0:
        return None
    return tuple(int(value) for value in np.mean(pixels, axis=0))


def percentile_color(pixels: np.ndarray, percentile: float) -> tuple[int, int, int] | None:
    if pixels.size == 0:
        return None
    return tuple(int(value) for value in np.percentile(pixels, percentile, axis=0))


def title_fingerprint(title_or_payload: Any) -> str:
    if hasattr(title_or_payload, "to_dict"):
        payload = title_or_payload.to_dict()
    else:
        payload = title_or_payload
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cache_path(project_id: str, fingerprint: str, cache_root: Path | None = None) -> Path:
    root = cache_root or PATHS.title_style_cache
    safe_project = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(project_id)) or "global"
    return root / safe_project / f"{fingerprint[:32]}.json"


def get_cached_title_profile(project_id: str, title_fingerprint: str, cache_root: Path | None = None) -> TitleStyleProfile | None:
    path = _cache_path(project_id, title_fingerprint, cache_root)
    if not path.is_file():
        return None
    try:
        return TitleStyleProfile.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def save_title_profile(
    project_id: str,
    title_fingerprint: str,
    profile: TitleStyleProfile,
    cache_root: Path | None = None,
) -> None:
    path = _cache_path(project_id, title_fingerprint, cache_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
