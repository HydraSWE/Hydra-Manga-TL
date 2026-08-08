"""Phase 3 masking, cleaning, and exact-location text rendering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


@dataclass
class FittedText:
    lines: list[str]
    font_size: int
    box: list[int]
    line_height: int
    line_positions: list[list[int]] | None = None
    constraint_strategy: str = "rectangular"
    preserved_overlap_pixels: int = 0
    preserved_content_aware: bool = False
    fallback_reason: str = ""


def _text_bbox(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, **kwargs) -> tuple[int, int, int, int]:
    return draw.textbbox((0, 0), text, font=font, **kwargs)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, **kwargs) -> int:
    bounds = _text_bbox(draw, text, font, **kwargs)
    return bounds[2] - bounds[0]


def balance_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int = 0, stroke_width: int = 0) -> list[str]:
    """Wraps text to minimize line width variance for professional typography (Replaces _wrap)."""
    words = text.split()
    if not words:
        return []

    # ❌ FIXED: If any single word is wider than the max_width, this font size is too big. Fail immediately.
    for word in words:
        if _text_width(draw, word, font, stroke_width=stroke_width) > max_width:
            return []

    # 1. Greedy approach for basic fitting and long text
    greedy_lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if _text_width(draw, f"{current} {word}", font, stroke_width=stroke_width) <= max_width:
            current = f"{current} {word}"
        else:
            greedy_lines.append(current)
            current = word
    greedy_lines.append(current)

    # 2. Professional balancing for short manga dialogue
    if 1 < len(words) <= 20:
        best_layout = greedy_lines
        best_score = float("inf")

        target_lines = min(len(words), max_lines if max_lines > 0 else len(words))
        span_widths: dict[tuple[int, int], int] = {}
        for start in range(len(words)):
            for end in range(start + 1, len(words) + 1):
                span_widths[(start, end)] = _text_width(
                    draw,
                    " ".join(words[start:end]),
                    font,
                    stroke_width=stroke_width,
                )

        for line_count in range(1, target_lines + 1):
            costs: dict[tuple[int, int], tuple[float, list[tuple[int, int]]]] = {
                (0, 0): (0.0, [])
            }
            for used_lines in range(line_count):
                for start in range(len(words)):
                    state = costs.get((start, used_lines))
                    if state is None:
                        continue
                    remaining_lines = line_count - used_lines - 1
                    maximum_end = len(words) - remaining_lines
                    for end in range(start + 1, maximum_end + 1):
                        width = span_widths[(start, end)]
                        if width > max_width:
                            break
                        raggedness = float((max_width - width) ** 2)
                        if end == len(words):
                            raggedness = 0.0
                            if end - start == 1 and len(words) > 3:
                                raggedness += 2500.0
                        candidate_cost = state[0] + raggedness
                        key = (end, used_lines + 1)
                        previous = costs.get(key)
                        if previous is None or candidate_cost < previous[0]:
                            costs[key] = (
                                candidate_cost,
                                [*state[1], (start, end)],
                            )
            completed = costs.get((len(words), line_count))
            if completed is None:
                continue
            candidate = [" ".join(words[start:end]) for start, end in completed[1]]
            widths = [span_widths[span] for span in completed[1]]
            score = max(widths) - min(widths)
            if len(candidate[-1].split()) == 1 and len(words) > 3:
                score += 50
            if score < best_score:
                best_layout, best_score = candidate, score

        return best_layout if not max_lines or len(best_layout) <= max_lines else greedy_lines
        
    return greedy_lines if not max_lines or len(greedy_lines) <= max_lines else []

def fit_text(
    text: str, safe_box: list[int], font_path: Path, maximum: int = 72, minimum: int = 5,
    max_lines: int = 0,
) -> FittedText | None:
    """Uses a top-down shrinking approach within a pre-calculated safe rectangle."""
    width, height = safe_box[2] - safe_box[0], safe_box[3] - safe_box[1]
    if width < minimum or height < minimum:
        return None
    
    padding = int(min(width, height) * 0.15)
    usable_width = width - padding * 2
    usable_height = height - padding * 2
    if usable_width < minimum or usable_height < minimum:
        return None
        
    canvas = Image.new("L", (max(1, width), max(1, height)))
    draw = ImageDraw.Draw(canvas)

    for size in range(maximum, minimum - 1, -1):
        font = ImageFont.truetype(str(font_path), size)
        stroke_width = max(1, size // 10)
        lines = balance_lines(draw, text, font, usable_width, max_lines, stroke_width=stroke_width)
        
        if not lines:
            continue
            
        sample = _text_bbox(draw, "Ag", font, stroke_width=stroke_width)
        line_height = max(1, sample[3] - sample[1] + max(1, size // 5))
        total_height = line_height * len(lines)
        max_width = max((_text_width(draw, line, font, stroke_width=stroke_width) for line in lines), default=0)
        
        if max_width <= usable_width and total_height <= usable_height:
            return FittedText(lines, size, safe_box, line_height)
            
    return None


def has_preserved_content(group: dict) -> bool:
    return bool(_preserve_polygons(group))


def preserved_constraint_mask(
    size: tuple[int, int],
    group: dict,
    box: list[int],
    margin: int = 4,
) -> np.ndarray:
    width = max(1, int(box[2]) - int(box[0]))
    height = max(1, int(box[3]) - int(box[1]))
    mask = np.zeros((height, width), dtype=np.uint8)
    allowed_polygon = (
        group.get("placement_polygon")
        or group.get("polygon")
        or group.get("selection_polygon")
        or []
    )
    if allowed_polygon:
        allowed = np.zeros((height, width), dtype=np.uint8)
        points = np.asarray(allowed_polygon, dtype=np.int32)
        if points.ndim == 2 and points.shape[0] >= 3:
            shifted = points.copy()
            shifted[:, 0] = np.clip(shifted[:, 0] - int(box[0]), 0, width - 1)
            shifted[:, 1] = np.clip(shifted[:, 1] - int(box[1]), 0, height - 1)
            cv2.fillPoly(allowed, [shifted], 255)
            mask[allowed == 0] = 255
    image_width, image_height = size
    for polygon in _preserve_polygons(group):
        points = np.asarray(polygon, dtype=np.int32)
        if points.ndim != 2 or points.shape[0] < 3:
            continue
        shifted = points.copy()
        shifted[:, 0] = np.clip(shifted[:, 0] - int(box[0]), 0, width - 1)
        shifted[:, 1] = np.clip(shifted[:, 1] - int(box[1]), 0, height - 1)
        if (
            int(points[:, 0].max()) < int(box[0])
            or int(points[:, 0].min()) > int(box[2])
            or int(points[:, 1].max()) < int(box[1])
            or int(points[:, 1].min()) > int(box[3])
        ):
            continue
        cv2.fillPoly(mask, [shifted], 255)
    if margin > 0 and mask.any():
        radius = max(1, int(margin))
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (radius * 2 + 1, radius * 2 + 1),
        )
        mask = cv2.dilate(mask, kernel)
    if image_width <= 0 or image_height <= 0:
        return mask
    return mask


def _available_runs(blocked_columns: np.ndarray, left: int, right: int) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for x in range(left, right):
        blocked = bool(blocked_columns[x])
        if not blocked and start is None:
            start = x
        elif blocked and start is not None:
            runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, right))
    return runs


def _line_position_for_band(
    mask: np.ndarray,
    line_width: int,
    line_y: int,
    line_height: int,
    usable_left: int,
    usable_right: int,
    preferred_center: float,
) -> int | None:
    y1 = max(0, int(line_y))
    y2 = min(mask.shape[0], int(line_y + line_height))
    if y2 <= y1:
        return None
    blocked_columns = mask[y1:y2, :].any(axis=0)
    best_x: int | None = None
    best_score = float("inf")
    for run_left, run_right in _available_runs(blocked_columns, usable_left, usable_right):
        if run_right - run_left < line_width:
            continue
        candidate = int(round(preferred_center - line_width / 2))
        candidate = max(run_left, min(candidate, run_right - line_width))
        score = abs((candidate + line_width / 2) - preferred_center)
        if score < best_score:
            best_score = score
            best_x = candidate
    return best_x


def _distributed_line_positions(
    mask: np.ndarray,
    lines: list[str],
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont,
    *,
    stroke_width: int,
    line_height: int,
    usable_left: int,
    usable_top: int,
    usable_right: int,
    usable_bottom: int,
    preferred_center: float,
) -> list[list[int]] | None:
    if not lines:
        return []
    min_gap = max(1, line_height // 4)
    usable_height = max(1, usable_bottom - usable_top)
    if len(lines) == 1:
        preferred_y = [usable_top + max(0, (usable_height - line_height) // 2)]
    else:
        span = max(0, usable_height - line_height)
        preferred_y = [
            int(round(usable_top + span * (index / max(1, len(lines) - 1))))
            for index in range(len(lines))
        ]

    positions: list[list[int]] = []
    next_min_y = usable_top
    for index, line in enumerate(lines):
        remaining = len(lines) - index - 1
        max_y = usable_bottom - line_height - remaining * (line_height + min_gap)
        if max_y < next_min_y:
            return None
        candidates = list(range(next_min_y, max_y + 1))
        candidates.sort(key=lambda value: abs(value - preferred_y[index]))
        bounds = _text_bbox(draw, line, font, stroke_width=stroke_width)
        line_width = bounds[2] - bounds[0]
        selected: tuple[int, int] | None = None
        for line_y in candidates:
            line_left = _line_position_for_band(
                mask,
                line_width,
                line_y,
                line_height,
                usable_left,
                usable_right,
                preferred_center,
            )
            if line_left is None:
                continue
            selected = (line_left, line_y)
            break
        if selected is None:
            return None
        line_left, line_y = selected
        positions.append([int(line_left - bounds[0]), int(line_y)])
        next_min_y = int(line_y + line_height + min_gap)
    return positions


def _compact_line_positions(
    mask: np.ndarray,
    lines: list[str],
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont,
    *,
    stroke_width: int,
    line_height: int,
    usable_left: int,
    usable_top: int,
    usable_right: int,
    usable_bottom: int,
    preferred_center: float,
) -> list[list[int]] | None:
    total_height = line_height * len(lines)
    if total_height > usable_bottom - usable_top:
        return None
    center_y = usable_top + max(0, ((usable_bottom - usable_top) - total_height) // 2)
    max_y = usable_bottom - total_height
    y_candidates = list(range(usable_top, max_y + 1))
    y_candidates.sort(key=lambda value: abs(value - center_y))

    for y_start in y_candidates:
        positions: list[list[int]] = []
        valid = True
        for line_index, line in enumerate(lines):
            bounds = _text_bbox(draw, line, font, stroke_width=stroke_width)
            line_width = bounds[2] - bounds[0]
            line_y = y_start + line_index * line_height
            line_left = _line_position_for_band(
                mask,
                line_width,
                line_y,
                line_height,
                usable_left,
                usable_right,
                preferred_center,
            )
            if line_left is None:
                valid = False
                break
            positions.append([int(line_left - bounds[0]), int(line_y)])
        if valid:
            return positions
    return None


def fit_text_avoiding_preserved(
    text: str,
    safe_box: list[int],
    font_path: Path,
    group: dict,
    image_size: tuple[int, int],
    maximum: int = 72,
    minimum: int = 5,
    max_lines: int = 0,
) -> FittedText | None:
    width, height = safe_box[2] - safe_box[0], safe_box[3] - safe_box[1]
    if width < minimum or height < minimum or not has_preserved_content(group):
        return None

    padding = int(min(width, height) * 0.15)
    usable_left = padding
    usable_top = padding
    usable_right = width - padding
    usable_bottom = height - padding
    usable_width = usable_right - usable_left
    usable_height = usable_bottom - usable_top
    if usable_width < minimum or usable_height < minimum:
        return None

    canvas = Image.new("L", (max(1, width), max(1, height)))
    draw = ImageDraw.Draw(canvas)
    preferred_center = width / 2.0

    for size in range(maximum, minimum - 1, -1):
        font = ImageFont.truetype(str(font_path), size)
        stroke_width = max(1, size // 10)
        lines = balance_lines(
            draw,
            text,
            font,
            usable_width,
            max_lines,
            stroke_width=stroke_width,
        )
        if not lines:
            continue

        sample = _text_bbox(draw, "Ag", font, stroke_width=stroke_width)
        line_height = max(1, sample[3] - sample[1] + max(1, size // 5))
        total_height = line_height * len(lines)
        if total_height > usable_height:
            continue

        margin = max(3, size // 5)
        mask = preserved_constraint_mask(image_size, group, safe_box, margin=margin)
        for strategy, positions in (
            (
                "preserved_content_distributed",
                _distributed_line_positions(
                    mask,
                    lines,
                    draw,
                    font,
                    stroke_width=stroke_width,
                    line_height=line_height,
                    usable_left=usable_left,
                    usable_top=usable_top,
                    usable_right=usable_right,
                    usable_bottom=usable_bottom,
                    preferred_center=preferred_center,
                ),
            ),
            (
                "preserved_content_aware",
                _compact_line_positions(
                    mask,
                    lines,
                    draw,
                    font,
                    stroke_width=stroke_width,
                    line_height=line_height,
                    usable_left=usable_left,
                    usable_top=usable_top,
                    usable_right=usable_right,
                    usable_bottom=usable_bottom,
                    preferred_center=preferred_center,
                ),
            ),
        ):
            if positions is not None:
                return FittedText(
                    lines,
                    size,
                    safe_box,
                    line_height,
                    line_positions=positions,
                    constraint_strategy=strategy,
                    preserved_overlap_pixels=0,
                    preserved_content_aware=True,
                )

    return None


def _largest_inscribed_rectangle(mask: np.ndarray) -> tuple[int, int, int, int]:
    """Helper: Finds largest inscribed rectangle in a binary mask using histogram method."""
    rows, cols = mask.shape
    max_area, best_rect = 0, (0, 0, 0, 0)
    heights = np.zeros(cols, dtype=int)

    for i in range(rows):
        for j in range(cols):
            heights[j] = heights[j] + 1 if mask[i, j] else 0

        stack = []
        for j in range(cols + 1):
            h = heights[j] if j < cols else 0
            start = j
            while stack and stack[-1][1] > h:
                pos, height = stack.pop()
                area = height * (j - pos)
                if area > max_area:
                    max_area = area
                    best_rect = (pos, i - height + 1, j - pos, height)
                start = pos
            stack.append((start, h))
    return best_rect


def compute_safe_text_area(image: Image.Image, polygon: list[list[int]], base_padding: int = 5) -> list[int] | None:
    gray = np.asarray(image.convert("L"))
    height, width = gray.shape

    xs, ys = [int(p[0]) for p in polygon], [int(p[1]) for p in polygon]
    x1, y1 = max(0, min(xs)), max(0, min(ys))
    x2, y2 = min(width - 1, max(xs)), min(height - 1, max(ys))

    w, h = max(1, x2 - x1), max(1, y2 - y1)
    cx, cy = x1 + (w // 2), y1 + (h // 2)
    
    pad_x = max(50, min(300, int(w * 1.5)))
    pad_y = max(50, min(300, int(h * 1.5)))
    
    crop_x1, crop_y1 = max(0, cx - pad_x), max(0, cy - pad_y)
    crop_x2, crop_y2 = min(width, cx + pad_x), min(height, cy + pad_y)
    
    roi = gray[crop_y1:crop_y2, crop_x1:crop_x2]
    if roi.size == 0:
        return None

    blurred = cv2.GaussianBlur(roi, (5, 5), 0)
    binary = cv2.adaptiveThreshold(blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 41, 10)
    
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    
    best_label = -1
    best_score = float('inf')
    
    roi_x1, roi_y1 = x1 - crop_x1, y1 - crop_y1
    roi_x2, roi_y2 = x2 - crop_x1, y2 - crop_y1
    roi_cx, roi_cy = cx - crop_x1, cy - crop_y1
    ocr_area = w * h

    for i in range(1, count):
        bx, by, bw, bh, area = stats[i]
        
        if bw < w * 0.5 or bh < h * 0.5: 
            continue
            
        ix1, iy1 = max(bx, roi_x1), max(by, roi_y1)
        ix2, iy2 = min(bx + bw, roi_x2), min(by + bh, roi_y2)
        inter_area = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        
        coverage = inter_area / ocr_area
        
        if coverage < 0.4:
            continue

        ccx, ccy = centroids[i]
        
        # ❌ FIXED: Centroid rejection. If the center of the "bubble" is too far 
        # from the text, it's hallucinating the background (like jackets or hair).
        if abs(ccx - roi_cx) > max(80, w * 2.0) or abs(ccy - roi_cy) > max(80, h * 2.0):
            continue

        coverage_penalty = (1.0 - coverage) * 10.0
        dist_sq = (ccx - roi_cx)**2 + (ccy - roi_cy)**2
        dist_penalty = dist_sq / max(1, w**2 + h**2)
        area_penalty = area / max(1, ocr_area) * 0.1

        score = coverage_penalty + dist_penalty + area_penalty
        
        if score < best_score:
            best_score = score
            best_label = i

    if best_label == -1:
        return None

    bubble_mask = np.uint8(labels == best_label) * 255
    dist_map = cv2.distanceTransform(bubble_mask, cv2.DIST_L2, 5)
    
    bubble_w, bubble_h = stats[best_label, cv2.CC_STAT_WIDTH], stats[best_label, cv2.CC_STAT_HEIGHT]
    dynamic_margin = max(base_padding, int(min(bubble_w, bubble_h) * 0.08))
    
    safe_mask = np.uint8(dist_map >= dynamic_margin)

    if not safe_mask.any():
        return None

    rx, ry, rw, rh = _largest_inscribed_rectangle(safe_mask)
    
    if rw < 15 or rh < 15:
        return None

    final_x1, final_y1 = crop_x1 + rx, crop_y1 + ry
    return [final_x1, final_y1, final_x1 + rw, final_y1 + rh]


def detect_bubble_box(image: Image.Image, polygon: list[list[int]], padding: int = 5) -> list[int] | None:
    """Find a light connected speech-bubble interior surrounding a text group."""
    gray = np.asarray(image.convert("L"))
    height, width = gray.shape
    
    xs, ys = [int(point[0]) for point in polygon], [int(point[1]) for point in polygon]
    x1, y1, x2, y2 = max(0, min(xs)), max(0, min(ys)), min(width - 1, max(xs)), min(height - 1, max(ys))
    
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    binary = cv2.adaptiveThreshold(
        blurred, 1, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 41, 10
    )
    
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    
    source_area = max(1, (x2 - x1) * (y2 - y1))
    source_width, source_height = max(1, x2 - x1), max(1, y2 - y1)
    ocr_center_x, ocr_center_y = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    
    source_aspect = source_width / max(1, source_height)
    
    max_width = max(round(source_width * 5.0), source_width + 250)
    max_height = max(round(source_height * 7.5), source_height + 350)
    max_area = max(round(source_area * 15.0), source_area + 35_000)
    
    best_box = None
    best_score = float('inf')
    
    for label_idx in range(1, count):
        bx, by, bw, bh, area = [int(v) for v in stats[label_idx]]
        
        if bw < source_width or bh < source_height:
            continue
            
        if area < source_area * 1.15 or area > width * height * 0.55:
            continue
        if bw > max_width or bh > max_height or area > max_area:
            continue
            
        ix1, iy1 = max(bx, x1), max(by, y1)
        ix2, iy2 = min(bx + bw, x2), min(by + bh, y2)
        inter_w, inter_h = max(0, ix2 - ix1), max(0, iy2 - iy1)
        coverage = (inter_w * inter_h) / source_area
        
        if coverage < 0.8:
            continue
            
        cx, cy = centroids[label_idx]
        
        # ❌ FIXED: Same centroid rejection for the fallback tier.
        if abs(cx - ocr_center_x) > max(80, source_width * 2.0) or abs(cy - ocr_center_y) > max(80, source_height * 2.0):
            continue
            
        dist_sq = (cx - ocr_center_x) ** 2 + (cy - ocr_center_y) ** 2
        dist_score = dist_sq / (source_width ** 2 + source_height ** 2)
        coverage_score = (1.0 - coverage) * 10.0
        area_score = min(3.0, area / source_area)
        aspect_score = abs((bw / max(1, bh)) - source_aspect) * 2.0
        
        score = dist_score + coverage_score + area_score + aspect_score
        
        if score < best_score:
            best_score = score
            best_box = (bx, by, bw, bh)

    if best_box is None:
        return None
        
    bx, by, bw, bh = best_box
    left, top = bx + padding, by + padding
    right, bottom = bx + bw - padding, by + bh - padding
    
    if right - left < 20 or bottom - top < 12:
        return None
        
    return [left, top, right, bottom]

def _preserve_polygons(group: dict) -> list[list[list[int]]]:
    polygons: list[list[list[int]]] = []
    for polygon in group.get("preserve_polygons", []) or []:
        if polygon:
            polygons.append(polygon)
    for mark in group.get("preserved_marks", []) or []:
        if isinstance(mark, dict) and mark.get("preserve_policy", "preserve_original") == "preserve_original":
            polygon = mark.get("polygon")
            if polygon:
                polygons.append(polygon)
    return polygons


def _cleanup_clip_polygon(group: dict) -> list[list[int]]:
    return (
        group.get("cleanup_clip_polygon")
        or group.get("selection_polygon")
        or group.get("polygon")
        or []
    )


def _fill_polygons(mask: np.ndarray, polygons: list) -> None:
    for polygon in polygons or []:
        points = np.asarray(polygon, dtype=np.int32)
        if points.ndim != 2 or points.shape[0] < 3:
            continue
        cv2.fillPoly(mask, [points], 255)


def make_mask(size: tuple[int, int], groups: list[dict], dilation: int = 2) -> np.ndarray:
    width, height = size
    mask = np.zeros((height, width), dtype=np.uint8)
    for group in groups:
        group_mask = np.zeros((height, width), dtype=np.uint8)
        preserve_mask = np.zeros((height, width), dtype=np.uint8)
        polygons = (
            group.get("cleanup_polygons")
            or group.get("mask_polygons")
            or group.get("source_polygons")
            or []
        )
        _fill_polygons(group_mask, polygons)
        _fill_polygons(preserve_mask, _preserve_polygons(group))
        if dilation:
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilation * 2 + 1, dilation * 2 + 1))
            group_mask = cv2.dilate(group_mask, kernel)
            preserve_mask = cv2.dilate(preserve_mask, kernel)
        clip_polygon = _cleanup_clip_polygon(group)
        if clip_polygon:
            clip_mask = np.zeros((height, width), dtype=np.uint8)
            _fill_polygons(clip_mask, [clip_polygon])
            group_mask[clip_mask == 0] = 0
        group_mask[preserve_mask > 0] = 0
        mask = np.maximum(mask, group_mask)
    return mask


def clean_background(image: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, str]:
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    selected = mask > 0
    if not selected.any():
        return image.copy(), "unchanged"
    expanded = cv2.dilate(mask, np.ones((9, 9), np.uint8)) > 0
    ring = expanded & ~selected
    ring_pixels = image[ring]
    light_ratio = float(np.mean(gray[expanded] > 190)) if expanded.any() else 0.0
    if light_ratio >= 0.62 and len(ring_pixels):
        fill = np.percentile(ring_pixels, 75, axis=0).astype(np.uint8)
        cleaned = image.copy()
        cleaned[selected] = fill
        return cleaned, "light-background-fill"
    bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    cleaned = cv2.inpaint(bgr, mask, 3, cv2.INPAINT_TELEA)
    return cv2.cvtColor(cleaned, cv2.COLOR_BGR2RGB), "opencv-inpaint"


def render_group(
    image: Image.Image,
    fitted: FittedText,
    text: str,
    font_path: Path,
    *,
    color: str | None = None,
    alignment: str = "center",
    direction: str = "horizontal-ltr",
    angle: float = 0.0,
) -> dict:
    font = ImageFont.truetype(str(font_path), fitted.font_size)
    x1, y1, x2, y2 = fitted.box
    crop = np.asarray(image.crop((x1, y1, x2, y2)).convert("L"))
    light = float(crop.mean()) >= 130 if crop.size else True
    if color and color.startswith("#") and len(color) == 7:
        fill = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
    else:
        fill = (15, 15, 15) if light else (255, 255, 255)
    stroke_fill = (255, 255, 255) if light else (0, 0, 0)
    stroke_width = 0 if light else max(1, fitted.font_size // 10)
    total_height = fitted.line_height * len(fitted.lines)
    layer = Image.new("RGBA", (max(1, x2 - x1), max(1, y2 - y1)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    explicit_positions = fitted.line_positions or []
    y = max(0, int((y2 - y1 - total_height) / 2))
    positions = []
    for index, line in enumerate(fitted.lines):
        bounds = _text_bbox(draw, line, font, stroke_width=stroke_width)
        line_width = bounds[2] - bounds[0]
        if index < len(explicit_positions):
            x = int(explicit_positions[index][0])
            y_for_line = int(explicit_positions[index][1])
        elif alignment == "left":
            x = -bounds[0]
            y_for_line = y
        elif alignment == "right":
            x = (x2 - x1) - line_width - bounds[0]
            y_for_line = y
        else:
            x = max(0, (x2 - x1 - line_width) // 2) - bounds[0]
            y_for_line = y
        draw.text((x, y_for_line), line, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)
        positions.append([x1 + x, y1 + y_for_line])
        y += fitted.line_height
        
    if angle and abs(angle) > 0.01:
        rotated = layer.rotate(-angle, expand=True, resample=Image.Resampling.BICUBIC)
        center_x = x1 + (x2 - x1) / 2
        center_y = y1 + (y2 - y1) / 2
        paste_x = int(round(center_x - rotated.width / 2))
        paste_y = int(round(center_y - rotated.height / 2))
        if image.mode != "RGBA":
            base = image.convert("RGBA")
            base.alpha_composite(rotated, (paste_x, paste_y))
            image.paste(base.convert(image.mode))
        else:
            image.alpha_composite(rotated, (paste_x, paste_y))
    else:
        if image.mode != "RGBA":
            base = image.convert("RGBA")
            base.alpha_composite(layer, (x1, y1))
            image.paste(base.convert(image.mode))
        else:
            image.alpha_composite(layer, (x1, y1))
    return {
        "text": text,
        "lines": fitted.lines,
        "font_size": fitted.font_size,
        "box": fitted.box,
        "positions": positions,
        "preserved_content_aware": bool(fitted.preserved_content_aware),
        "preserved_overlap_pixels": int(fitted.preserved_overlap_pixels),
        "constraint_strategy": fitted.constraint_strategy,
        "preserved_overlap_fallback": fitted.fallback_reason == "preserved_overlap_fallback",
        "constraint_fallback_reason": fitted.fallback_reason,
    }

def expanded_box(polygon: list[list[int]], image_size: tuple[int, int]) -> list[int]:
    xs, ys = [point[0] for point in polygon], [point[1] for point in polygon]
    x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
    
    width, height = image_size
    box_width, box_height = max(1, x2 - x1), max(1, y2 - y1)
    
    is_vertical = box_height > box_width
    
    if is_vertical:
        # Vertical-to-horizontal: Keep text near the original bubble area
        # Expand narrow speech columns, but avoid turning larger bubbles into
        # panel-wide boxes that spill text outside the cleaned area.
        expansion = 1.8 if box_width < 120 else 1.22
        desired_width = min(width, max(80, int(box_width * expansion)))
        pad_y = max(5, int(box_height * 0.08))
    else:
        pad_x = max(3, round(box_width * 0.12))
        desired_width = max(44, box_width + (pad_x * 2))
        pad_y = max(2, round(box_height * 0.06))

    desired_width = min(width - 4, desired_width)
    center_x = (x1 + x2) // 2
    
    left = center_x - desired_width // 2
    right = left + desired_width
    
    if left < 0:
        left = 0
        right = min(width, desired_width)
    elif right > width:
        right = width
        left = max(0, right - desired_width)

    top = max(0, y1 - pad_y)
    bottom = min(height, y2 + pad_y)
    
    return [int(left), int(top), int(right), int(bottom)]
