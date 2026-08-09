"""Text fitting and preserved-content constraints for phase 3 rendering."""

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


