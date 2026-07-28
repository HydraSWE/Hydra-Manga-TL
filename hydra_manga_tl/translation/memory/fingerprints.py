"""Deterministic text identities and forward-compatible OCR-region fingerprints."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re
import unicodedata
from typing import Iterable

import numpy as np
from PIL import Image, ImageDraw


TEXT_HASH_PREFIX = "tmtext:v1:"
REGION_HASH_PREFIX = "phash64:v1:"
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)


def normalize_tm_source_text(text: str) -> str:
    """Normalize formatting without changing linguistic content.

    NFKC deliberately folds safe width variants, including full-width Latin
    punctuation. Punctuation, casing, and OCR-recognized characters otherwise
    remain part of the exact-match identity.
    """

    value = str(text).replace("\r\n", "\n").replace("\r", "\n")
    value = unicodedata.normalize("NFKC", value)
    return _WHITESPACE_RE.sub(" ", value).strip()


def source_text_hash(text: str) -> str:
    normalized = normalize_tm_source_text(text)
    return TEXT_HASH_PREFIX + sha256(normalized.encode("utf-8")).hexdigest()


def _dct_basis(size: int = 32, coefficients: int = 8) -> np.ndarray:
    positions = np.arange(size, dtype=np.float64)
    frequencies = np.arange(coefficients, dtype=np.float64)[:, None]
    basis = np.cos((np.pi / (2.0 * size)) * (2.0 * positions + 1.0) * frequencies)
    basis[0, :] *= np.sqrt(1.0 / size)
    basis[1:, :] *= np.sqrt(2.0 / size)
    return basis


_PHASH_BASIS = _dct_basis()


def _masked_union_crop(
    image: Image.Image,
    polygons: Iterable[Iterable[Iterable[int | float]]],
) -> Image.Image | None:
    normalized_polygons: list[list[tuple[int, int]]] = []
    for polygon in polygons:
        points = [(int(point[0]), int(point[1])) for point in polygon if len(point) >= 2]
        if len(points) >= 3:
            normalized_polygons.append(points)
    if not normalized_polygons:
        return None

    xs = [point[0] for polygon in normalized_polygons for point in polygon]
    ys = [point[1] for polygon in normalized_polygons for point in polygon]
    left = max(0, min(xs))
    top = max(0, min(ys))
    right = min(image.width, max(xs) + 1)
    bottom = min(image.height, max(ys) + 1)
    if right <= left or bottom <= top:
        return None

    rgb = image.convert("RGB")
    mask = Image.new("L", rgb.size, 0)
    draw = ImageDraw.Draw(mask)
    for polygon in normalized_polygons:
        draw.polygon(polygon, fill=255)
    white = Image.new("RGB", rgb.size, "white")
    masked = Image.composite(rgb, white, mask)
    return masked.crop((left, top, right, bottom))


def source_region_hash(
    image: str | Path | Image.Image,
    polygons: Iterable[Iterable[Iterable[int | float]]],
) -> str | None:
    """Return a versioned 64-bit perceptual hash for region metadata.

    TM v1 stores this value but never consults it during lookup.
    """

    opened: Image.Image | None = None
    try:
        if isinstance(image, Image.Image):
            source = image
        else:
            opened = Image.open(Path(image))
            source = opened
        crop = _masked_union_crop(source, polygons)
        if crop is None:
            return None
        grayscale = crop.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
        pixels = np.asarray(grayscale, dtype=np.float64)
        low_frequency = _PHASH_BASIS @ pixels @ _PHASH_BASIS.T
        median = float(np.median(low_frequency))
        bits = (low_frequency >= median).reshape(-1)
        value = 0
        for enabled in bits:
            value = (value << 1) | int(bool(enabled))
        return f"{REGION_HASH_PREFIX}{value:016x}"
    except (OSError, TypeError, ValueError, IndexError):
        return None
    finally:
        if opened is not None:
            opened.close()
