"""Style extraction engine for HSTR."""

from __future__ import annotations

import math

import cv2
import numpy as np
from PIL import Image

from .models import TitleObject
from .style_profile import (
    FillProfile,
    GlowProfile,
    GradientProfile,
    OutlineProfile,
    ShadowProfile,
    TitleStyleProfile,
    TypographyProfile,
)
from .utils import average_color, dominant_color, masked_pixels


def _mask_array(mask: Image.Image) -> np.ndarray:
    return np.asarray(mask.convert("L")) > 0


def _edge_and_core(mask: Image.Image, box: list[int]) -> tuple[np.ndarray, np.ndarray]:
    x1, y1, x2, y2 = box
    crop = np.asarray(mask.crop((x1, y1, x2, y2)).convert("L"))
    selected = crop > 0
    if not selected.any():
        return selected, selected
    kernel = np.ones((3, 3), dtype=np.uint8)
    eroded = cv2.erode(np.uint8(selected) * 255, kernel, iterations=1) > 0
    edge = selected & ~eroded
    core = eroded if eroded.any() else selected
    return edge, core


def extract_fill(image: Image.Image, mask: Image.Image, title: TitleObject) -> FillProfile:
    pixels = masked_pixels(image, mask, title.box)
    if pixels.size == 0:
        return FillProfile()
    luminance = pixels @ np.asarray([0.2126, 0.7152, 0.0722])
    bright = pixels[luminance >= np.percentile(luminance, 55)]
    sample = bright if len(bright) else pixels
    return FillProfile(
        dominant_color=dominant_color(sample),
        average_color=average_color(sample),
        colors=[color for color in (dominant_color(sample), average_color(sample)) if color],
    )


def extract_outline(image: Image.Image, mask: Image.Image, title: TitleObject) -> OutlineProfile:
    x1, y1, x2, y2 = title.box
    edge, core = _edge_and_core(mask, title.box)
    crop = np.asarray(image.crop((x1, y1, x2, y2)).convert("RGB"))
    if crop.size == 0:
        return OutlineProfile()
    if not edge.any():
        pixels = crop.reshape(-1, 3)
        return OutlineProfile(color=dominant_color(pixels), width=1.0)
    edge_pixels = crop[edge]
    core_pixels = crop[core] if core.any() else crop.reshape(-1, 3)
    edge_luma = edge_pixels @ np.asarray([0.2126, 0.7152, 0.0722])
    core_luma = core_pixels @ np.asarray([0.2126, 0.7152, 0.0722])
    darker_edge = edge_pixels[edge_luma <= np.percentile(edge_luma, 45)]
    color = dominant_color(darker_edge if len(darker_edge) else edge_pixels)
    fill_luma = float(np.mean(core_luma)) if len(core_luma) else 128.0
    edge_luma_mean = float(np.mean(edge_luma)) if len(edge_luma) else fill_luma
    width = 2.0 if abs(fill_luma - edge_luma_mean) >= 20 else 1.0
    distance = cv2.distanceTransform(np.uint8(_mask_array(mask)) * 255, cv2.DIST_L2, 3)
    region_distance = distance[y1:y2, x1:x2]
    if region_distance.size and region_distance.max() > 0:
        width = max(width, min(12.0, float(np.percentile(region_distance[region_distance > 0], 22))))
    return OutlineProfile(color=color, width=round(width, 2), colors=[color] if color else [])


def extract_shadow(image: Image.Image, mask: Image.Image, title: TitleObject) -> ShadowProfile:
    pixels = masked_pixels(image, mask, title.box)
    if pixels.size == 0:
        return ShadowProfile()
    luminance = pixels @ np.asarray([0.2126, 0.7152, 0.0722])
    dark = pixels[luminance <= np.percentile(luminance, 12)]
    return ShadowProfile(color=dominant_color(dark) if len(dark) else None)


def extract_glow(image: Image.Image, mask: Image.Image, title: TitleObject) -> GlowProfile:
    x1, y1, x2, y2 = title.box
    source = np.asarray(image.convert("RGB"))
    selected = _mask_array(mask)
    expanded = cv2.dilate(np.uint8(selected) * 255, np.ones((9, 9), np.uint8), iterations=1) > 0
    ring = expanded & ~selected
    ring_pixels = source[ring]
    if ring_pixels.size == 0:
        return GlowProfile()
    saturation = ring_pixels.max(axis=1) - ring_pixels.min(axis=1)
    colorful = ring_pixels[saturation >= np.percentile(saturation, 70)]
    radius = max(0.0, min(8.0, math.sqrt(max(1, (x2 - x1) * (y2 - y1))) / 40.0))
    return GlowProfile(color=dominant_color(colorful if len(colorful) else ring_pixels), radius=round(radius, 2), opacity=0.5)


def extract_gradient(image: Image.Image, mask: Image.Image, title: TitleObject) -> GradientProfile:
    pixels = masked_pixels(image, mask, title.box)
    if pixels.size == 0:
        return GradientProfile(kind="solid")
    colors = [color for color in (dominant_color(pixels), average_color(pixels)) if color]
    spread = float(np.mean(np.std(pixels.astype(np.float32), axis=0)))
    return GradientProfile(kind="linear" if spread >= 35.0 else "solid", colors=colors)


def analyze_typography(title: TitleObject) -> TypographyProfile:
    text = title.original_text or title.translated_text
    categories: list[str] = []
    width = max(1, title.box[2] - title.box[0])
    height = max(1, title.box[3] - title.box[1])
    if height > width * 1.5:
        categories.append("condensed")
    if text.isupper():
        categories.append("bold")
    return TypographyProfile(family_hint="comic" if categories else None, weight="bold" if "bold" in categories else None, categories=categories)


def estimate_rotation(title: TitleObject) -> float | None:
    points = title.polygon
    if len(points) < 2:
        return None
    dx = points[1][0] - points[0][0]
    dy = points[1][1] - points[0][1]
    angle = math.degrees(math.atan2(dy, dx))
    return round(angle, 2) if abs(angle) >= 1.0 else 0.0


def extract_title_style(image: Image.Image, mask: Image.Image, title: TitleObject) -> TitleStyleProfile:
    return TitleStyleProfile(
        fill=extract_fill(image, mask, title),
        outline=extract_outline(image, mask, title),
        shadow=extract_shadow(image, mask, title),
        glow=extract_glow(image, mask, title),
        gradient=extract_gradient(image, mask, title),
        typography=analyze_typography(title),
        rotation=estimate_rotation(title),
        opacity=1.0,
        blend_mode="normal",
        metadata={"extractor": "hstr-v1"},
    )
