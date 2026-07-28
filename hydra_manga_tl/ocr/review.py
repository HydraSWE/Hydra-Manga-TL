"""Build OCR-only review projects for Hydra AI data collection."""

from __future__ import annotations

import json
from pathlib import Path

from hydra_manga_tl.project.discovery import discover
from hydra_manga_tl.phase.layout import classify_text_group, group_regions
from hydra_manga_tl.ocr.service import OCRService
from hydra_manga_tl.core.paths import PATHS
from hydra_manga_tl.phase.pipeline import _classify_bubble
from hydra_manga_tl.project.model import MangaProject
from hydra_manga_tl.core.settings import SETTINGS


def create_ocr_review_project(source: Path, name: str | None = None, limit: int = 0) -> MangaProject:
    source = source.resolve()
    images = list(discover(source)) if source.is_dir() else [source]
    if limit > 0:
        images = images[:limit]
    if not images:
        raise ValueError(f"No supported images found in {source}")

    project = MangaProject.create(name or f"{source.name} OCR Review", PATHS.projects)
    project.root = str((PATHS.projects / project.id).resolve())
    project.source_language = "Japanese"
    project.quality = "Balanced"
    project.literal_provider = "marian"
    project.localization_provider = "local"
    project.localization_style = "Manga"
    project.text_style = "Manga"
    project.add_sources([(path.resolve(), str(path.resolve().relative_to(source)) if source.is_dir() else path.name) for path in images])
    project.save()

    ocr_service = OCRService(
        ("japan",),
        use_subprocess=SETTINGS.ocr_subprocess_enabled,
        memory_limit_mb=SETTINGS.ocr_worker_memory_limit_mb,
        retry_stats_path=PATHS.cache / "ocr_retry_stats.json",
    )
    project.artifacts.mkdir(parents=True, exist_ok=True)
    for index, image in enumerate(project.images, 1):
        path = Path(image.source_path)
        print(f"[{index}/{len(project.images)}] OCR review: {path.name}", flush=True)
        service_result = ocr_service.analyze_page(
            path,
            preferred_language="japan",
            quality=project.quality,
            auto_language_fallback=False,
        )
        ocr_result = service_result.ocr_result
        regions = service_result.final_regions
        groups = group_regions(regions)
        translated_groups = []
        for group_index, group in enumerate(groups, 1):
            confidence = min(float(regions[item - 1]["confidence"]) for item in group.member_indices)
            polygons = [regions[item - 1]["polygon"] for item in group.member_indices]
            classification = classify_text_group(group, regions, ocr_result.model_language)
            bubble_type = _classify_bubble(group)
            if bubble_type not in {"credit", "sfx"}:
                bubble_type = classification.kind
            translated_groups.append({
                "index": group_index,
                "original_text": group.text.strip(),
                "literal_text": group.text.strip(),
                "translated_text": "",
                "ocr_confidence": confidence,
                "polygon": [[group.bbox[0], group.bbox[1]], [group.bbox[2], group.bbox[1]], [group.bbox[2], group.bbox[3]], [group.bbox[0], group.bbox[3]]],
                "status": "review",
                "review_reasons": ["ocr_review_candidate", *classification.reasons],
                "alternatives": [],
                "provider": "ocr-review",
                "model": ocr_result.model_language,
                "localization_style": project.localization_style,
                "translation_quality": "review",
                "localization_note": "OCR-only review candidate; translate after OCR approval.",
                "member_region_indices": group.member_indices,
                "source_direction": group.direction,
                "direction": "horizontal-ltr",
                "source_polygons": polygons,
                "source_member_texts": [str(regions[item - 1].get("text", "")) for item in group.member_indices],
                "bubble_type": bubble_type,
            })
        ocr_path = project.artifacts / f"{image.id}_ocr.json"
        ocr_path.write_text(json.dumps(ocr_result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        translation_path = project.artifacts / f"{image.id}_translated_en.json"
        translation_path.write_text(json.dumps({
            "source": str(path.resolve()),
            "source_language": "Japanese",
            "target_language": project.target_language,
            "ocr_model_language": ocr_result.model_language,
            "source_regions": regions,
            "translation_groups": translated_groups,
            "literal_provider": "ocr-review",
            "localization_provider": "local",
            "localization_style": project.localization_style,
            "text_style": project.text_style,
            "bubble_padding": project.bubble_padding,
            "max_lines": project.max_lines,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        image.status = "review"
        image.source_language = "Japanese"
        image.ocr_result = str(ocr_path)
        image.translation_result = str(translation_path)
        project.save()
    return project
