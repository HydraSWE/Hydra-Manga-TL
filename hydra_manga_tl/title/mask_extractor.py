"""Deterministic glyph-level masks for title cleanup."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

import cv2
import numpy as np
from PIL import Image

from .utils import box_from_polygon


@dataclass
class TitleGlyphMaskResult:
    mask: np.ndarray
    method: str
    confidence: float
    coverage: float
    warning: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    selected_candidate: str = ""
    component_summary: dict[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return not self.warning and self.confidence >= 0.55 and np.count_nonzero(self.mask) > 0

    def report(self) -> dict[str, Any]:
        return {
            "title_mask_method": self.method,
            "title_mask_confidence": round(float(self.confidence), 3),
            "title_mask_coverage": round(float(self.coverage), 4),
            "title_mask_warning": self.warning,
            "needs_title_mask_review": not self.accepted,
            "title_mask_candidates": self.candidates,
            "title_mask_selected_candidate": self.selected_candidate or self.method,
            "title_mask_component_summary": self.component_summary,
        }


@dataclass
class MaskCandidate:
    name: str
    mask: np.ndarray
    component_count: int
    largest_ratio: float
    edge_density: float = 0.0
    color_confidence: float = 0.0


_MASK_CACHE_VERSION = "title-mask-reconstruct-v1"
_MASK_CACHE: dict[str, TitleGlyphMaskResult] = {}


def _empty(size: tuple[int, int], method: str, warning: str) -> TitleGlyphMaskResult:
    return TitleGlyphMaskResult(np.zeros((size[1], size[0]), dtype=np.uint8), method, 0.0, 0.0, warning, selected_candidate=method)


def _cache_key(group: dict[str, Any], size: tuple[int, int]) -> str:
    payload = {
        "version": _MASK_CACHE_VERSION,
        "size": list(size),
        "project_id": group.get("project_id", ""),
        "original_text": group.get("original_text") or group.get("text") or "",
        "source_polygons": group.get("source_polygons") or [group.get("polygon", [])],
        "style_profile": group.get("style_profile", {}),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_polygons(polygons: Any) -> list[list[list[int]]]:
    if not isinstance(polygons, list) or not polygons:
        return []
    if isinstance(polygons[0], list) and polygons[0] and isinstance(polygons[0][0], (int, float)):
        polygons = [polygons]
    normalized: list[list[list[int]]] = []
    for polygon in polygons:
        if not isinstance(polygon, list) or len(polygon) < 3:
            continue
        try:
            normalized.append([[int(x), int(y)] for x, y in polygon])
        except (TypeError, ValueError):
            continue
    return normalized


def _mask_from_polygons(size: tuple[int, int], polygons: list[list[list[int]]], dilation: int = 0) -> np.ndarray:
    width, height = size
    mask = np.zeros((height, width), dtype=np.uint8)
    for polygon in polygons:
        cv2.fillPoly(mask, [np.asarray(polygon, dtype=np.int32)], 255)
    if dilation > 0 and np.count_nonzero(mask):
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilation * 2 + 1, dilation * 2 + 1))
        mask = cv2.dilate(mask, kernel)
    return mask


def _combined_box(polygons: list[list[list[int]]], size: tuple[int, int], pad: int = 8) -> list[int] | None:
    if not polygons:
        return None
    boxes = [box_from_polygon(polygon) for polygon in polygons]
    x1 = max(0, min(box[0] for box in boxes) - pad)
    y1 = max(0, min(box[1] for box in boxes) - pad)
    x2 = min(size[0], max(box[2] for box in boxes) + pad)
    y2 = min(size[1], max(box[3] for box in boxes) + pad)
    if x2 <= x1 or y2 <= y1:
        return None
    return [x1, y1, x2, y2]


def _coverage(mask: np.ndarray, box: list[int]) -> float:
    x1, y1, x2, y2 = box
    area = max(1, (x2 - x1) * (y2 - y1))
    return float(np.count_nonzero(mask[y1:y2, x1:x2]) / area)


def _component_mask(
    candidate: np.ndarray,
    crop_area: int,
    *,
    allow_large_title_glyphs: bool = False,
) -> tuple[np.ndarray, int, float, dict[str, int]]:
    label_count, labels, stats, _ = cv2.connectedComponentsWithStats(candidate, 8)
    kept = np.zeros_like(candidate)
    count = 0
    largest_ratio = 0.0
    summary = {"accepted": 0, "small": 0, "large": 0, "non_text": 0}
    for label in range(1, label_count):
        x, y, w, h, area = (int(value) for value in stats[label])
        if area < 12:
            summary["small"] += 1
            continue
        ratio = area / max(1, w * h)
        if area / max(1, crop_area) > 0.65:
            largest_ratio = max(largest_ratio, area / max(1, crop_area))
            summary["large"] += 1
            continue
        if allow_large_title_glyphs:
            text_like = (
                0.035 <= ratio <= 1.0
                and 2 <= w <= 320
                and 2 <= h <= 280
                and area / max(1, crop_area) <= 0.45
            )
        else:
            text_like = (
                0.06 <= ratio <= 1.0
                and 2 <= w <= 260
                and 2 <= h <= 120
                and not (w > 180 and h > 100)
            )
        if not text_like:
            summary["non_text"] += 1
            continue
        kept[labels == label] = 255
        count += 1
        summary["accepted"] += 1
        largest_ratio = max(largest_ratio, area / max(1, crop_area))
    return kept, count, largest_ratio, summary


def _solidify_components(mask: np.ndarray) -> np.ndarray:
    """Fill the interiors of disconnected, already-filtered outline shapes."""
    if not np.count_nonzero(mask):
        return mask.copy()
    solid = np.zeros_like(mask)
    label_count, labels = cv2.connectedComponents(mask, 8)
    for label in range(1, label_count):
        component = np.uint8(labels == label) * 255
        contours, _hierarchy = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            cv2.drawContours(solid, contours, -1, 255, thickness=cv2.FILLED)
    return solid


def _candidate_from_crop(crop: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    hue = hsv[:, :, 0]
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    colorful_print = (sat >= 65) & (val >= 70)
    white_print = (sat <= 70) & (val >= 200)
    dark_print = val <= 80
    edges = cv2.Canny(gray, 55, 150)
    edge_band = cv2.dilate(edges, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))) > 0
    candidate = (colorful_print | ((white_print | dark_print) & edge_band)).astype(np.uint8) * 255
    candidate = cv2.morphologyEx(candidate, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2)))
    candidate = cv2.morphologyEx(candidate, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    return candidate


def _crop_channels(crop: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    return hsv[:, :, 1], hsv[:, :, 2], gray, cv2.Canny(gray, 55, 150)


def _candidate_variants_from_crop(crop: np.ndarray, expansion: int) -> dict[str, np.ndarray]:
    sat, val, _gray, edges = _crop_channels(crop)
    edge_band = cv2.dilate(edges, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))) > 0
    fill = ((sat >= 65) & (val >= 70)).astype(np.uint8) * 255
    white_print = (sat <= 70) & (val >= 200)
    outline = ((white_print | (val <= 80)) & edge_band).astype(np.uint8) * 255
    row = _candidate_from_crop(crop)
    merged = np.maximum(fill, outline)
    merged = cv2.morphologyEx(merged, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    print_fill = np.maximum(fill, white_print.astype(np.uint8) * 255)
    complete_print = np.maximum(merged, print_fill)
    complete_print = cv2.morphologyEx(complete_print, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    stroke = fill.copy()
    if expansion > 0 and np.count_nonzero(stroke):
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (expansion * 2 + 1, expansion * 2 + 1))
        stroke = cv2.dilate(stroke, kernel)
    conservative = cv2.dilate(row, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)), iterations=1) if np.count_nonzero(row) else row
    return {
        "fill-color": fill,
        "print-fill": print_fill,
        "complete-print": complete_print,
        "outline-edge": outline,
        "stroke-expanded": stroke,
        "row-component": row,
        "conservative-merged": np.maximum(conservative, merged),
    }


def _evaluate(
    mask: np.ndarray,
    box: list[int],
    component_count: int,
    largest_ratio: float,
    method: str,
    *,
    allow_complete_outline: bool = False,
) -> TitleGlyphMaskResult:
    coverage = _coverage(mask, box)
    if component_count < 2:
        return TitleGlyphMaskResult(mask, method, 0.25, coverage, "too_few_text_components")
    if coverage < 0.004:
        return TitleGlyphMaskResult(mask, method, 0.25, coverage, "mask_too_sparse")
    complete_method = method in {"outline-solid", "outline-complete", "complete-print"}
    coverage_limit = 0.62 if allow_complete_outline and complete_method else 0.38
    if coverage > coverage_limit:
        return TitleGlyphMaskResult(mask, method, 0.2, coverage, "mask_too_broad")
    if largest_ratio > 0.45 and coverage > 0.22:
        return TitleGlyphMaskResult(mask, method, 0.25, coverage, "component_too_large")
    confidence = min(0.95, 0.5 + min(0.25, component_count * 0.025) + min(0.2, coverage * 1.8))
    return TitleGlyphMaskResult(mask, method, confidence, coverage)


def _style_expansion(group: dict[str, Any]) -> int:
    profile = group.get("style_profile") if isinstance(group.get("style_profile"), dict) else {}
    outline = profile.get("outline") if isinstance(profile.get("outline"), dict) else {}
    stroke = profile.get("stroke") if isinstance(profile.get("stroke"), dict) else {}
    width = max(float(outline.get("width", 0) or 0), float(stroke.get("width", 0) or 0))
    return max(0, min(4, int(round(width))))


def _color_confidence(group: dict[str, Any]) -> float:
    colors = group.get("source_text_colors") or group.get("source_member_colors") or []
    if not isinstance(colors, list) or not colors:
        return 0.0
    valid = 0
    for color in colors:
        if isinstance(color, (list, tuple)) and len(color) >= 3:
            try:
                high = max(int(value) for value in color[:3])
                low = min(int(value) for value in color[:3])
            except (TypeError, ValueError):
                continue
            if high - low >= 35:
                valid += 1
    return min(1.0, valid / max(1, len(colors)))


def _edge_density(mask: np.ndarray, image: Image.Image) -> float:
    if not np.count_nonzero(mask):
        return 0.0
    gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 55, 150)
    selected = mask > 0
    return float(np.count_nonzero(edges[selected]) / max(1, np.count_nonzero(selected)))


def _extract_box_candidates(image: Image.Image, box: list[int], size: tuple[int, int], group: dict[str, Any]) -> list[MaskCandidate]:
    x1, y1, x2, y2 = box
    crop = np.asarray(image.crop((x1, y1, x2, y2)).convert("RGB"))
    if crop.size == 0:
        return []
    hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
    white_pixels = (hsv[:, :, 1] <= 70) & (hsv[:, :, 2] >= 200)
    candidates: list[MaskCandidate] = []
    for name, raw in _candidate_variants_from_crop(crop, _style_expansion(group)).items():
        complete_manual_title = bool(group.get("manual") and group.get("render_mode") == "art_text")
        kept, component_count, largest_ratio, _summary = _component_mask(
            raw,
            crop.shape[0] * crop.shape[1],
            allow_large_title_glyphs=complete_manual_title and name in {"outline-edge", "print-fill", "complete-print"},
        )
        variants = [(name, kept, component_count, largest_ratio)]
        if name == "outline-edge" and np.count_nonzero(kept):
            solid = _solidify_components(kept)
            solid, solid_count, solid_largest, _solid_summary = _component_mask(
                solid,
                crop.shape[0] * crop.shape[1],
                allow_large_title_glyphs=complete_manual_title,
            )
            variants.append(("outline-solid", solid, solid_count, solid_largest))
            if complete_manual_title:
                nearby = cv2.dilate(
                    kept,
                    cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (41, 41)),
                    iterations=1,
                ) > 0
                complete = np.maximum(solid, np.uint8(white_pixels & nearby) * 255)
                complete, complete_count, complete_largest, _complete_summary = _component_mask(
                    complete,
                    crop.shape[0] * crop.shape[1],
                    allow_large_title_glyphs=True,
                )
                variants.append(("outline-complete", complete, complete_count, complete_largest))
        for variant_name, variant, variant_count, variant_largest in variants:
            full = np.zeros((size[1], size[0]), dtype=np.uint8)
            full[y1:y2, x1:x2] = variant
            if np.count_nonzero(full):
                full = cv2.dilate(full, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)), iterations=1)
            candidates.append(MaskCandidate(
                name=variant_name,
                mask=full,
                component_count=variant_count,
                largest_ratio=variant_largest,
                color_confidence=_color_confidence(group),
            ))
    return candidates


def _merge_candidates(name: str, candidates: list[MaskCandidate], size: tuple[int, int]) -> MaskCandidate:
    full = np.zeros((size[1], size[0]), dtype=np.uint8)
    count = 0
    largest = 0.0
    color = 0.0
    for candidate in candidates:
        full = np.maximum(full, candidate.mask)
        count += candidate.component_count
        largest = max(largest, candidate.largest_ratio)
        color = max(color, candidate.color_confidence)
    return MaskCandidate(name, full, count, largest, color_confidence=color)


def _component_summary(candidates: list[MaskCandidate]) -> dict[str, Any]:
    return {
        "candidate_count": len(candidates),
        "accepted_components": sum(candidate.component_count for candidate in candidates),
        "largest_component_ratio": round(max((candidate.largest_ratio for candidate in candidates), default=0.0), 4),
    }


def _candidate_report(result: TitleGlyphMaskResult, candidate: MaskCandidate) -> dict[str, Any]:
    return {
        "name": candidate.name,
        "accepted": result.accepted,
        "confidence": round(float(result.confidence), 3),
        "coverage": round(float(result.coverage), 4),
        "warning": result.warning,
        "component_count": candidate.component_count,
        "largest_component_ratio": round(float(candidate.largest_ratio), 4),
        "color_confidence": round(float(candidate.color_confidence), 3),
    }


def _choose_candidate(image: Image.Image, box: list[int], candidates: list[MaskCandidate], *, prefer_complete: bool = False) -> TitleGlyphMaskResult:
    reports: list[dict[str, Any]] = []
    evaluated: list[tuple[float, TitleGlyphMaskResult, MaskCandidate]] = []
    for candidate in candidates:
        result = _evaluate(
            candidate.mask,
            box,
            candidate.component_count,
            candidate.largest_ratio,
            candidate.name,
            allow_complete_outline=prefer_complete,
        )
        if result.accepted:
            result.confidence = min(0.98, result.confidence + min(0.06, candidate.color_confidence * 0.06) + min(0.04, _edge_density(candidate.mask, image) * 0.2))
        reports.append(_candidate_report(result, candidate))
        if prefer_complete and candidate.name == "outline-complete":
            penalty_start = 0.62
        elif prefer_complete and candidate.name in {"outline-solid", "complete-print"}:
            penalty_start = 0.52
        else:
            penalty_start = 0.24
        score = result.confidence - max(0.0, result.coverage - penalty_start) * 0.8
        if prefer_complete and result.accepted:
            score += min(0.18, result.coverage * 0.55)
            if candidate.name == "conservative-merged":
                score += 0.08
            elif candidate.name == "outline-complete":
                score += 0.16
            elif candidate.name == "complete-print":
                score += 0.14
            elif candidate.name == "outline-solid":
                score += 0.12
            elif candidate.name == "stroke-expanded":
                score += 0.05
        evaluated.append((score, result, candidate))
    evaluated.sort(key=lambda item: (item[1].accepted, item[0]), reverse=True)
    if not evaluated:
        return TitleGlyphMaskResult(np.zeros((image.size[1], image.size[0]), dtype=np.uint8), "opencv-glyph", 0.0, 0.0, "missing_title_geometry")
    _score, selected, candidate = evaluated[0]
    selected.method = "opencv-glyph"
    selected.candidates = reports
    selected.selected_candidate = candidate.name
    selected.component_summary = _component_summary(candidates)
    return selected


def extract_title_glyph_mask(image: Image.Image, group: dict[str, Any], image_size: tuple[int, int] | None = None) -> TitleGlyphMaskResult:
    size = image_size or image.size
    explicit = _normalize_polygons(group.get("mask_polygons") or group.get("cleanup_polygons"))
    if explicit:
        box = _combined_box(explicit, size, pad=0)
        if box is None:
            return _empty(size, "explicit-polygons", "invalid_explicit_mask")
        mask = _mask_from_polygons(size, explicit, dilation=1)
        source_box = _combined_box(_normalize_polygons(group.get("source_polygons") or [group.get("polygon", [])]), size, pad=0) or box
        coverage = _coverage(mask, source_box)
        confidence = 0.95 if 0.002 <= coverage <= 0.55 else 0.35
        warning = "" if confidence >= 0.55 else "explicit_mask_coverage_out_of_range"
        return TitleGlyphMaskResult(
            mask,
            "explicit-polygons",
            confidence,
            coverage,
            warning,
            candidates=[{"name": "explicit-polygons", "accepted": warning == "", "confidence": confidence, "coverage": round(coverage, 4), "warning": warning}],
            selected_candidate="explicit-polygons",
            component_summary={"candidate_count": 1, "accepted_components": 1 if warning == "" else 0},
        )

    source_polygons = _normalize_polygons(group.get("source_polygons") or [group.get("polygon", [])])
    box = _combined_box(source_polygons, size, pad=10)
    if box is None:
        return _empty(size, "opencv-glyph", "missing_title_geometry")
    source_box = _combined_box(source_polygons, size, pad=0) or box
    source_coverage = sum(
        max(1, (box_from_polygon(polygon)[2] - box_from_polygon(polygon)[0]) * (box_from_polygon(polygon)[3] - box_from_polygon(polygon)[1]))
        for polygon in source_polygons
    ) / max(1, (source_box[2] - source_box[0]) * (source_box[3] - source_box[1]))
    manual_reconstruction = bool(group.get("manual") and group.get("render_mode") == "art_text")
    if len(source_polygons) <= 1 and source_coverage > 0.82 and not manual_reconstruction:
        return _empty(size, "opencv-glyph", "broad_title_box_requires_glyph_evidence")

    key = _cache_key(group, size)
    cached = _MASK_CACHE.get(key)
    if cached is not None:
        return TitleGlyphMaskResult(
            cached.mask.copy(),
            cached.method,
            cached.confidence,
            cached.coverage,
            cached.warning,
            candidates=list(cached.candidates),
            selected_candidate=cached.selected_candidate,
            component_summary={**cached.component_summary, "cache": "hit"},
        )

    per_box_candidates: list[MaskCandidate] = []
    boxes = [_combined_box([polygon], size, pad=6) for polygon in source_polygons] if len(source_polygons) > 1 else [box]
    for item in boxes:
        if item is None:
            continue
        per_box_candidates.extend(_extract_box_candidates(image, item, size, group))
    if not per_box_candidates and len(source_polygons) > 1:
        per_box_candidates.extend(_extract_box_candidates(image, box, size, group))
    grouped: dict[str, list[MaskCandidate]] = {}
    for candidate in per_box_candidates:
        grouped.setdefault(candidate.name, []).append(candidate)
    merged = [_merge_candidates(name, items, size) for name, items in grouped.items()]
    if "conservative-merged" not in grouped and per_box_candidates:
        merged.append(_merge_candidates("conservative-merged", per_box_candidates, size))
    selected = _choose_candidate(image, box, merged, prefer_complete=manual_reconstruction)
    if selected.accepted:
        _MASK_CACHE[key] = TitleGlyphMaskResult(
            selected.mask.copy(),
            selected.method,
            selected.confidence,
            selected.coverage,
            selected.warning,
            candidates=list(selected.candidates),
            selected_candidate=selected.selected_candidate,
            component_summary=dict(selected.component_summary),
        )
    return selected
