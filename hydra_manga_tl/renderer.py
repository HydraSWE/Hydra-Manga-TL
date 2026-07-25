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
        
        target_lines = max_lines if max_lines > 0 else len(words)
        
        for line_count in range(1, target_lines + 1):
            candidates: list[list[str]] = []

            def split(start: int, remaining: int, current_lines: list[str]) -> None:
                if remaining == 1:
                    if start < len(words): 
                        candidates.append(current_lines + [" ".join(words[start:])])
                    return
                for end in range(start + 1, len(words) - remaining + 2):
                    split(end, remaining - 1, current_lines + [" ".join(words[start:end])])

            split(0, line_count, [])
            for candidate in candidates:
                widths = [_text_width(draw, line, font, stroke_width=stroke_width) for line in candidate]
                if widths and max(widths) <= max_width:
                    score = max(widths) - min(widths) 
                    # Penalize orphan words on the last line
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

def make_mask(size: tuple[int, int], groups: list[dict], dilation: int = 2) -> np.ndarray:
    width, height = size
    mask = np.zeros((height, width), dtype=np.uint8)
    for group in groups:
        for polygon in group["source_polygons"]:
            points = np.asarray(polygon, dtype=np.int32)
            cv2.fillPoly(mask, [points], 255)
    if dilation:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilation * 2 + 1, dilation * 2 + 1))
        mask = cv2.dilate(mask, kernel)
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
    
    y = max(0, int((y2 - y1 - total_height) / 2))
    positions = []
    for line in fitted.lines:
        bounds = _text_bbox(draw, line, font, stroke_width=stroke_width)
        line_width = bounds[2] - bounds[0]
        if alignment == "left":
            x = -bounds[0]
        elif alignment == "right":
            x = (x2 - x1) - line_width - bounds[0]
        else:
            x = max(0, (x2 - x1 - line_width) // 2) - bounds[0]
        draw.text((x, y), line, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)
        positions.append([x1 + x, y1 + y])
        y += fitted.line_height
        
    if image.mode != "RGBA":
        base = image.convert("RGBA")
        base.alpha_composite(layer, (x1, y1))
        image.paste(base.convert(image.mode))
    else:
        image.alpha_composite(layer, (x1, y1))
    return {"text": text, "lines": fitted.lines, "font_size": fitted.font_size, "box": fitted.box, "positions": positions}

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
