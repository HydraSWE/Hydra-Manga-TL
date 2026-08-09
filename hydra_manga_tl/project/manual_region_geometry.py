"""Geometry and OCR-mark helpers for manual regions."""

from __future__ import annotations

import re
import unicodedata

from hydra_manga_tl.phase.layout import TextGroup


def normalize_image_rect(start, end, image_size: tuple[int, int], minimum: int = 8) -> list[int] | None:
    """Normalize, clamp, and validate a rectangle in source-image coordinates."""
    width, height = image_size
    x1, x2 = sorted((round(float(start[0])), round(float(end[0]))))
    y1, y2 = sorted((round(float(start[1])), round(float(end[1]))))
    rect = [max(0, x1), max(0, y1), min(width, x2), min(height, y2)]
    if rect[2] - rect[0] < minimum or rect[3] - rect[1] < minimum:
        return None
    return rect


def rect_to_polygon(rect: list[int] | tuple[int, int, int, int]) -> list[list[int]]:
    """Return a four-point polygon for a rectangular region."""
    x1, y1, x2, y2 = [int(value) for value in rect]
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def polygon_bounding_rect(polygon: list[list[int]] | tuple[tuple[int, int], ...]) -> list[int] | None:
    """Return the bounding rectangle for a polygon, or None for invalid input."""
    if len(polygon) < 3:
        return None
    xs = [int(point[0]) for point in polygon]
    ys = [int(point[1]) for point in polygon]
    rect = [min(xs), min(ys), max(xs), max(ys)]
    if rect[2] <= rect[0] or rect[3] <= rect[1]:
        return None
    return rect


def normalize_image_polygon(
    points: list[list[float]] | tuple[tuple[float, float], ...],
    image_size: tuple[int, int],
    *,
    minimum: int = 8,
) -> list[list[int]] | None:
    """Clamp and validate a polygon in source-image coordinates."""
    if len(points) < 3:
        return None
    width, height = image_size
    polygon: list[list[int]] = []
    for point in points:
        x = max(0, min(width, round(float(point[0]))))
        y = max(0, min(height, round(float(point[1]))))
        if not polygon or polygon[-1] != [x, y]:
            polygon.append([x, y])
    if len(polygon) > 1 and polygon[0] == polygon[-1]:
        polygon.pop()
    rect = polygon_bounding_rect(polygon)
    if rect is None or rect[2] - rect[0] < minimum or rect[3] - rect[1] < minimum:
        return None
    if polygon_area(polygon) <= 0:
        return None
    if polygon_self_intersects(polygon):
        return None
    return polygon


def normalize_geometry_polygon(value: object) -> list[list[int]]:
    polygon: list[list[int]] = []
    if not isinstance(value, (list, tuple)):
        return polygon
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            polygon.append([int(point[0]), int(point[1])])
        except (TypeError, ValueError):
            continue
    return polygon


def normalize_geometry_polygons(value: object) -> list[list[list[int]]]:
    polygons: list[list[list[int]]] = []
    if not isinstance(value, (list, tuple)):
        return polygons
    for polygon in value:
        normalized = normalize_geometry_polygon(polygon)
        if normalized:
            polygons.append(normalized)
    return polygons


def cleanup_polygons_from_ocr_regions(source_regions: list[dict]) -> list[list[list[int]]]:
    """Return OCR-region polygons for cleanup without reusing the user selection."""
    return normalize_geometry_polygons([
        region.get("polygon", [])
        for region in source_regions
        if isinstance(region, dict)
    ])


def cleanup_polygons_for_manual_region(
    source_regions: list[dict],
    composed: TextGroup,
    selection_polygon: list[list[int]],
) -> list[list[list[int]]]:
    cleanup = cleanup_polygons_from_ocr_regions(source_regions)
    if cleanup:
        return cleanup
    if composed.bbox and len(composed.bbox) == 4:
        return [rect_to_polygon(composed.bbox)]
    return [selection_polygon]


def _compact_mark_text(text: object) -> str:
    return re.sub(r"\s+", "", str(text or "").strip())


def _is_decorative_mark_char(char: str) -> bool:
    codepoint = ord(char)
    if char in "♪♫♬♩♡♥❤★☆◇◆■□●○◎※〆〒":
        return True
    if unicodedata.category(char).startswith("S"):
        return True
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or 0xFE00 <= codepoint <= 0xFE0F
    )


def _is_preservable_mark_text(text: object) -> bool:
    compact = _compact_mark_text(text)
    if not compact:
        return False
    stripped = compact.strip(".,!?！？。…・ッっー~〜-")
    if not stripped:
        return False
    return all(_is_decorative_mark_char(char) for char in stripped)


def _region_bbox(polygon: list[list[int]]) -> list[int]:
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def decorative_symbols_from_ocr_regions(source_regions: list[dict]) -> list[dict]:
    symbols: list[dict] = []
    for index, region in enumerate(source_regions, 1):
        if not isinstance(region, dict) or not _is_preservable_mark_text(region.get("text", "")):
            continue
        polygon = normalize_geometry_polygon(region.get("polygon", []))
        if not polygon:
            continue
        symbols.append({
            "id": f"symbol:{index}",
            "kind": "decorative_symbol",
            "text": str(region.get("text", "")),
            "confidence": float(region.get("confidence", 0.0) or 0.0),
            "polygon": polygon,
            "bbox": _region_bbox(polygon),
            "render_policy": "redraw_semantic",
            "source": "ocr",
        })
    return symbols


def preserved_marks_from_ocr_regions(source_regions: list[dict]) -> list[dict]:
    return []


def split_manual_source_regions(source_regions: list[dict]) -> tuple[list[dict], list[dict]]:
    text_regions: list[dict] = []
    mark_regions: list[dict] = []
    for region in source_regions:
        if isinstance(region, dict) and _is_preservable_mark_text(region.get("text", "")):
            mark_regions.append(region)
        else:
            text_regions.append(region)
    if not text_regions:
        return source_regions, []
    return text_regions, decorative_symbols_from_ocr_regions(mark_regions)


def polygon_area(polygon: list[list[int]]) -> float:
    area = 0.0
    for index, point in enumerate(polygon):
        nxt = polygon[(index + 1) % len(polygon)]
        area += point[0] * nxt[1] - nxt[0] * point[1]
    return area / 2.0


def polygon_self_intersects(polygon: list[list[int]]) -> bool:
    if len(polygon) < 4:
        return False

    def orientation(a: list[int], b: list[int], c: list[int]) -> int:
        value = (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])
        if value == 0:
            return 0
        return 1 if value > 0 else 2

    def on_segment(a: list[int], b: list[int], c: list[int]) -> bool:
        return (
            min(a[0], c[0]) <= b[0] <= max(a[0], c[0])
            and min(a[1], c[1]) <= b[1] <= max(a[1], c[1])
        )

    def intersects(a: list[int], b: list[int], c: list[int], d: list[int]) -> bool:
        o1 = orientation(a, b, c)
        o2 = orientation(a, b, d)
        o3 = orientation(c, d, a)
        o4 = orientation(c, d, b)
        if o1 != o2 and o3 != o4:
            return True
        return (
            (o1 == 0 and on_segment(a, c, b))
            or (o2 == 0 and on_segment(a, d, b))
            or (o3 == 0 and on_segment(c, a, d))
            or (o4 == 0 and on_segment(c, b, d))
        )

    edges = [
        (polygon[index], polygon[(index + 1) % len(polygon)])
        for index in range(len(polygon))
    ]
    for i, (a, b) in enumerate(edges):
        for j, (c, d) in enumerate(edges):
            if abs(i - j) <= 1 or {i, j} == {0, len(edges) - 1}:
                continue
            if intersects(a, b, c, d):
                return True
    return False


def overlapping_auto_indices(groups: list[dict], rect: list[int]) -> list[int]:
    x1, y1, x2, y2 = rect
    hits: list[int] = []
    for group in groups:
        if group.get("manual"):
            continue
        polygon = group.get("polygon") or []
        if not polygon:
            continue
        bbox = polygon_bounding_rect(polygon)
        if not bbox:
            continue
        bx1, by1, bx2, by2 = bbox
        if bx2 <= x1 or bx1 >= x2 or by2 <= y1 or by1 >= y2:
            continue
        hits.append(int(group.get("index", 0)))
    return hits
