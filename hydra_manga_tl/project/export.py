"""Chapter export helpers for image folders, archives, and comic-book files."""

from __future__ import annotations

import shutil
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Callable

from PIL import Image

from hydra_manga_tl.project.artifacts import target_slug


IMAGE_FORMATS = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP"}
EXPORT_MODES = {"original", "cleaned", "ocr-only", "translated", "side-by-side"}
ProgressCallback = Callable[[int, int], None]


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


def _multi_target(project) -> bool:
    return len(set(getattr(project, "target_languages", []) or [])) > 1


def _target_folder(project, destination: Path) -> Path:
    if not _multi_target(project):
        return destination
    return destination / target_slug(project.target_language)


def _target_file_base(project, destination: Path) -> Path:
    if not _multi_target(project):
        return destination
    slug = target_slug(project.target_language)
    return destination.with_name(f"{destination.stem}_{slug}")


def exported_archive_path(
    project,
    destination: Path,
    archive_format: str = "zip",
) -> Path:
    suffix = ".cbz" if archive_format.lower() == "cbz" else ".zip"
    return _target_file_base(project, destination).with_suffix(suffix)


def exported_pdf_path(project, destination: Path) -> Path:
    return _target_file_base(project, destination).with_suffix(".pdf")


def exported_image_path(project, image, destination: Path, image_format: str) -> Path:
    destination = _target_folder(project, destination)
    return destination / Path(image.relative_path).with_suffix(
        f".{_format_extension(image_format.lower())}"
    )


def export_image(
    project,
    image,
    destination: Path,
    *,
    mode: str = "translated",
    image_format: str = "png",
) -> Path | None:
    mode = mode if mode in EXPORT_MODES else "translated"
    image_format = image_format.lower()
    if image_format not in IMAGE_FORMATS:
        image_format = "png"
    source = _export_source(image, mode)
    if source is None:
        return None
    target = exported_image_path(project, image, destination, image_format)
    target.parent.mkdir(parents=True, exist_ok=True)
    if image_format == "png":
        shutil.copy2(source, target)
    else:
        with Image.open(source) as opened:
            converted = opened.convert("RGB")
            converted.save(target, IMAGE_FORMATS[image_format], quality=94)
    return target


def export_images(
    project,
    destination: Path,
    *,
    mode: str = "translated",
    image_format: str = "png",
    progress_callback: ProgressCallback | None = None,
) -> int:
    mode = mode if mode in EXPORT_MODES else "translated"
    image_format = image_format.lower()
    if image_format not in IMAGE_FORMATS:
        image_format = "png"
    count = 0
    total = len(project.images)
    for current, image in enumerate(project.images, start=1):
        count += int(
            export_image(
                project,
                image,
                destination,
                mode=mode,
                image_format=image_format,
            )
            is not None
        )
        if progress_callback is not None:
            progress_callback(current, total)
    return count


def export_archive(
    project,
    destination: Path,
    *,
    mode: str = "translated",
    image_format: str = "png",
    archive_format: str = "zip",
    progress_callback: ProgressCallback | None = None,
) -> Path:
    archive_path = exported_archive_path(project, destination, archive_format)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    total = len(project.images)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for current, image in enumerate(project.images, start=1):
            source = _export_source(image, mode)
            if source is None:
                if progress_callback is not None:
                    progress_callback(current, total)
                continue
            arcname = Path(image.relative_path).with_suffix(f".{_format_extension(image_format.lower())}").as_posix()
            if image_format.lower() == source.suffix.lstrip(".").lower():
                archive.write(source, arcname)
                if progress_callback is not None:
                    progress_callback(current, total)
                continue
            with Image.open(source) as opened:
                converted = opened.convert("RGB")
                with BytesIO() as buffer:
                    converted.save(
                        buffer,
                        IMAGE_FORMATS.get(image_format.lower(), "PNG"),
                        quality=94,
                    )
                    archive.writestr(arcname, buffer.getvalue())
            if progress_callback is not None:
                progress_callback(current, total)
    return archive_path


def export_pdf(
    project,
    destination: Path,
    *,
    mode: str = "translated",
    dpi: int = 150,
    quality: int = 92,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    """Export ordered RGB pages with source pixel dimensions and sRGB policy."""
    destination = exported_pdf_path(project, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pages: list[Image.Image] = []
    total = len(project.images) + 1
    try:
        for current, image in enumerate(project.images, start=1):
            source = _export_source(image, mode)
            if source is None:
                if progress_callback is not None:
                    progress_callback(current, total)
                continue
            with Image.open(source) as opened:
                pages.append(opened.convert("RGB").copy())
            if progress_callback is not None:
                progress_callback(current, total)
        if not pages:
            raise ValueError("No pages are available for PDF export.")
        first, remaining = pages[0], pages[1:]
        first.save(
            destination,
            "PDF",
            save_all=True,
            append_images=remaining,
            resolution=max(72, int(dpi)),
            quality=max(60, min(100, int(quality))),
            optimize=True,
            title=str(getattr(project, "name", "Hydra Manga TL")),
            subject=(
                f"Hydra Manga TL - target "
                f"{target_slug(getattr(project, 'target_language', 'en'))}"
            ),
        )
        if progress_callback is not None:
            progress_callback(total, total)
    finally:
        for page in pages:
            page.close()
    return destination.resolve()
