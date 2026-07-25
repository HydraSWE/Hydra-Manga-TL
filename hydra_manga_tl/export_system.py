"""Chapter export helpers for image folders, archives, and comic-book files."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from PIL import Image


IMAGE_FORMATS = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP"}
EXPORT_MODES = {"original", "cleaned", "ocr-only", "translated", "side-by-side"}


def _mode_source(image, mode: str) -> Path:
    if mode == "original":
        return Path(image.source_path)
    if mode == "side-by-side":
        return Path(image.preview_image)
    if mode == "translated":
        return Path(image.rendered_image)
    if mode == "cleaned":
        return Path(image.rendered_image).with_name(f"{Path(image.source_path).stem}_cleaned.png")
    if mode == "ocr-only":
        return Path(image.rendered_image).with_name(f"{Path(image.source_path).stem}_mask.png")
    return Path(image.rendered_image)


def _export_source(image, mode: str) -> Path | None:
    source = _mode_source(image, mode)
    if source.is_file():
        return source
    if mode == "translated":
        original = Path(image.source_path)
        if original.is_file():
            return original
    return None


def _format_extension(image_format: str) -> str:
    return "jpg" if image_format == "jpeg" else image_format


def export_images(project, destination: Path, *, mode: str = "translated", image_format: str = "png") -> int:
    mode = mode if mode in EXPORT_MODES else "translated"
    image_format = image_format.lower()
    if image_format not in IMAGE_FORMATS:
        image_format = "png"
    count = 0
    for image in project.images:
        source = _export_source(image, mode)
        if source is None:
            continue
        target = destination / Path(image.relative_path).with_suffix(f".{_format_extension(image_format)}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if image_format == "png":
            shutil.copy2(source, target)
        else:
            with Image.open(source) as opened:
                converted = opened.convert("RGB")
                converted.save(target, IMAGE_FORMATS[image_format], quality=94)
        count += 1
    return count


def export_archive(project, destination: Path, *, mode: str = "translated", image_format: str = "png", archive_format: str = "zip") -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    suffix = ".cbz" if archive_format.lower() == "cbz" else ".zip"
    archive_path = destination.with_suffix(suffix)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for image in project.images:
            source = _export_source(image, mode)
            if source is None:
                continue
            arcname = Path(image.relative_path).with_suffix(f".{_format_extension(image_format.lower())}").as_posix()
            if image_format.lower() == source.suffix.lstrip(".").lower():
                archive.write(source, arcname)
                continue
            with Image.open(source) as opened:
                converted = opened.convert("RGB")
                temp_path = project.artifacts / "_export_tmp" / arcname
                temp_path.parent.mkdir(parents=True, exist_ok=True)
                converted.save(temp_path, IMAGE_FORMATS.get(image_format.lower(), "PNG"), quality=94)
                archive.write(temp_path, arcname)
    return archive_path
