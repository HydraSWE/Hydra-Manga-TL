"""Adaptive image preprocessing for OCR-ready manga pages."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageFilter, ImageOps


@dataclass(frozen=True)
class PageQuality:
    width: int
    height: int
    contrast: float
    sharpness: float
    noise: float
    orientation: str
    operations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PreprocessedImage:
    source_path: str
    ocr_path: str
    same_geometry: bool
    quality: PageQuality

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["quality"] = self.quality.to_dict()
        return payload


def _estimate_noise(gray: np.ndarray) -> float:
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    residual = gray.astype(np.float32) - blurred.astype(np.float32)
    return float(np.mean(np.abs(residual)))


def assess_page_quality(image: Image.Image) -> PageQuality:
    rgb = image.convert("RGB")
    gray = np.asarray(rgb.convert("L"))
    contrast = float(gray.std())
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    noise = _estimate_noise(gray)
    width, height = rgb.size
    orientation = "portrait" if height >= width else "landscape"
    operations: list[str] = []
    if contrast < 42:
        operations.append("autocontrast")
    if noise > 9:
        operations.append("denoise")
    if sharpness < 95:
        operations.append("sharpen")
    if min(width, height) < 900:
        operations.append("small_text_upscale_recommended")
    if orientation == "landscape" and width > height * 1.25:
        operations.append("orientation_review")
    return PageQuality(width, height, contrast, sharpness, noise, orientation, operations)


def prepare_ocr_image(source: Path, output_dir: Path) -> PreprocessedImage:
    """Create a same-size OCR-enhanced image and return quality metadata.

    The first v0.8 preprocessing pass intentionally preserves page geometry so
    OCR polygons still line up with the source page. Orientation changes and
    full-page upscaling are recorded as recommendations until the pipeline has
    coordinate remapping for every downstream stage.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        image = opened.convert("RGB")
    quality = assess_page_quality(image)
    processed = image

    if "autocontrast" in quality.operations:
        processed = ImageOps.autocontrast(processed)
    if "denoise" in quality.operations:
        bgr = cv2.cvtColor(np.asarray(processed), cv2.COLOR_RGB2BGR)
        denoised = cv2.fastNlMeansDenoisingColored(bgr, None, 5, 5, 7, 21)
        processed = Image.fromarray(cv2.cvtColor(denoised, cv2.COLOR_BGR2RGB))
    if "sharpen" in quality.operations:
        processed = processed.filter(ImageFilter.UnsharpMask(radius=1.2, percent=130, threshold=3))

    ocr_path = output_dir / f"{source.stem}_ocr_preprocessed.png"
    processed.save(ocr_path)
    return PreprocessedImage(
        source_path=str(source.resolve()),
        ocr_path=str(ocr_path.resolve()),
        same_geometry=processed.size == image.size,
        quality=quality,
    )
