"""Target-isolated project artifact paths with legacy compatibility."""

from __future__ import annotations

from pathlib import Path
import re


def target_slug(target: str) -> str:
    value = re.sub(r"[^a-z0-9_-]+", "-", str(target).strip().casefold())
    return value.strip("-") or "en"


def target_root(artifacts: Path, target: str) -> Path:
    return Path(artifacts) / "targets" / target_slug(target)


def target_manifest_path(artifacts: Path, target: str) -> Path:
    return target_root(artifacts, target) / "chapter_job_manifest.json"


def target_translation_path(
    artifacts: Path,
    image_id: str,
    target: str,
) -> Path:
    slug = target_slug(target)
    return target_root(artifacts, slug) / f"{image_id}_translated_{slug}.json"


def target_render_dir(
    artifacts: Path,
    image_id: str,
    target: str,
) -> Path:
    return target_root(artifacts, target) / image_id


def rendered_filename(source: Path, target: str) -> str:
    return f"{Path(source).stem}_translated_{target_slug(target)}.png"
