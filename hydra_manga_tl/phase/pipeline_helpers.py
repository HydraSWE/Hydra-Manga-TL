"""Pure pipeline helper functions and resume fingerprints."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from hydra_manga_tl import __version__
from hydra_manga_tl.core.normalization import normalize_global_text
from hydra_manga_tl.core.paths import PATHS
from hydra_manga_tl.core.region_types import normalize_region_type
from hydra_manga_tl.phase.pipeline_config import project_pipeline_config
from hydra_manga_tl.ocr.core import FULL_PAGE_OCR_MAX_SIDE, clean_ocr_text
from hydra_manga_tl.phase.job_manifest import JobManifest
from hydra_manga_tl.phase.layout import (
    TextGroup,
    classify_text_group,
    decorative_symbols_from_regions,
    is_decorative_mark_text,
)
from hydra_manga_tl.phase.state_manager import (
    PipelineDependencyGraph,
    PipelineStateManager,
    StageContract,
)
from hydra_manga_tl.project.artifacts import (
    rendered_filename,
    target_render_dir,
    target_root,
    target_translation_path,
)
from hydra_manga_tl.translation.cache_store import TRANSLATION_CACHE
from hydra_manga_tl.translation.engines import PageDialogue

def _source_text_color(image: Image.Image, polygon: list[list[int]]) -> list[int] | None:
    if not polygon:
        return None
    xs = [int(point[0]) for point in polygon]
    ys = [int(point[1]) for point in polygon]
    box = [max(0, min(xs)), max(0, min(ys)), min(image.size[0], max(xs)), min(image.size[1], max(ys))]
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    crop = image.crop(tuple(box)).convert("RGB")
    mask = Image.new("L", crop.size, 0)
    shifted = [(int(x) - box[0], int(y) - box[1]) for x, y in polygon]
    ImageDraw.Draw(mask).polygon(shifted, fill=255)
    crop_pixels = crop.load()
    mask_pixels = mask.load()
    pixels = [
        crop_pixels[x, y]
        for y in range(crop.height)
        for x in range(crop.width)
        if mask_pixels[x, y]
    ]
    if not pixels:
        return None

    def bucket(pixel: tuple[int, int, int]) -> tuple[int, int, int]:
        return tuple(int(component // 24) * 24 for component in pixel)

    colored_candidates: dict[tuple[int, int, int], int] = {}
    fallback_candidates: dict[tuple[int, int, int], int] = {}
    for red, green, blue in pixels:
        high = max(red, green, blue)
        low = min(red, green, blue)
        saturation = high - low
        luma = 0.2126 * red + 0.7152 * green + 0.0722 * blue
        key = bucket((red, green, blue))
        if saturation >= 45 and 35 <= luma <= 245:
            colored_candidates[key] = colored_candidates.get(key, 0) + 1
        elif luma <= 95 or luma >= 215:
            fallback_candidates[key] = fallback_candidates.get(key, 0) + 1
    candidates = colored_candidates if sum(colored_candidates.values()) >= max(8, len(pixels) * 0.015) else fallback_candidates
    if not candidates:
        return None
    color = max(candidates.items(), key=lambda item: item[1])[0]
    return [int(value) for value in color]


def _text_group_without_preserved_marks(
    group: TextGroup,
    source_regions: list[dict],
) -> tuple[TextGroup, list[int], list[dict]]:
    text_indices: list[int] = []
    mark_regions: list[dict] = []
    for member_index in group.member_indices:
        if not (1 <= member_index <= len(source_regions)):
            continue
        region = source_regions[member_index - 1]
        if is_decorative_mark_text(region.get("text", "")):
            mark_regions.append(region)
        else:
            text_indices.append(member_index)
    if not text_indices:
        return group, list(group.member_indices), []

    boxes = []
    member_texts: list[str] = []
    confidences: list[float] = []
    for member_index in text_indices:
        region = source_regions[member_index - 1]
        polygon = region.get("polygon", [])
        xs = [point[0] for point in polygon]
        ys = [point[1] for point in polygon]
        boxes.append([min(xs), min(ys), max(xs), max(ys)])
        member_texts.append(str(region.get("text", "")))
        confidences.append(float(region.get("confidence", 1.0) or 1.0))

    effective = TextGroup(
        member_indices=text_indices,
        text=clean_ocr_text("".join(member_texts)),
        bbox=[
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        ],
        direction=group.direction,
        source_member_texts=member_texts,
        confidence=sum(confidences) / max(1, len(confidences)),
    )
    return effective, text_indices, decorative_symbols_from_regions(mark_regions)


def _classify_bubble(group: TextGroup) -> str:
    """Patch 2: Conservative bubble classification to prevent false positives."""
    w = group.bbox[2] - group.bbox[0]
    h = max(1, group.bbox[3] - group.bbox[1])
    
    # Highly confident credit check: Must be extremely wide and physically large
    if w / h > 5.0 and w > 300: 
        return "credit"
        
    # Highly confident SFX check: Must be very short text but massive in pixel size
    clean = group.text.strip(".,!?\"'()[]{}<>-_~= ")
    if len(clean) <= 2 and (w > 120 or h > 150): 
        return "sfx"
        
    return classify_text_group(group).kind


def _auto_translate_region_type(region_type: str, config: dict[str, Any]) -> bool:
    kind = normalize_region_type(region_type)
    if kind == "dialogue":
        return True
    return bool(config.get(f"translate_{kind}", True))


def _initial_ocr_language(requested_source: str, quality: str, configured: str | None = None) -> str | None:
    if requested_source != "auto":
        return {"Chinese": "ch", "Japanese": "japan", "Latin-script": "en"}.get(requested_source)
    if quality == "Maximum":
        return None
    return configured or "japan"


def _needs_auto_ocr_fallback(ocr_result) -> bool:
    if ocr_result.language != "unknown" and ocr_result.language_confidence >= 0.70:
        return False
    return (
        ocr_result.average_ocr_confidence < 0.50
        or ocr_result.language_confidence < 0.55
        or ocr_result.language == "unknown"
    )


def _ocr_engine_languages(requested_source: str, quality: str, preferred: str | None) -> tuple[str, ...]:
    if requested_source != "auto":
        return tuple(language for language in (preferred,) if language)
    if quality == "Maximum":
        return ("japan", "ch", "en")
    return (preferred or "japan",)


def _stable_json_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_fragment(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return clean[:64] or "image"


def _stable_bubble_id(project_id: str, image_id: str, ordinal: int) -> str:
    workspace = str(project_id or "workspace").strip() or "workspace"
    return f"{workspace}:{image_id}:b{int(ordinal):04d}"


def _bubble_display_id(page_number: int, ordinal: int) -> str:
    return f"page_{int(page_number):03d}/bubble_{int(ordinal):03d}"


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _ocr_cache_key(source: Path, requested_source: str, quality: str, preferred: str | None) -> str:
    return _stable_json_hash({
        "kind": "ocr-v2",
        "image_sha256": _file_digest(source),
        "requested_source": requested_source,
        "quality": quality,
        "preferred_language": preferred,
        "full_page_ocr_max_side": FULL_PAGE_OCR_MAX_SIDE,
    })


def _page_translation_cache_key(
    page: PageDialogue,
    config: dict[str, Any],
    target: str,
    cache_store=None,
) -> str:
    cache_store = cache_store or TRANSLATION_CACHE
    return cache_store.page_translation_key(page, config, target)


def _render_stage_fingerprint(
    source: Path,
    translation_path: Path,
    *,
    target: str,
    config: dict[str, Any] | None = None,
) -> str:
    translation_payload = json.loads(
        translation_path.read_text(encoding="utf-8")
    )
    translation_payload.pop("render_review", None)
    translation_payload.pop("ai_review", None)
    return _stable_json_hash({
        "kind": "render-stage-v1",
        "pipeline_version": __version__,
        "source_sha256": _file_digest(source),
        "translation_payload": translation_payload,
        "target": target,
        "resume_policy": _resume_policy_payload(config or {}, target),
    })


def _resume_policy_payload(
    config: dict[str, Any],
    target: str,
) -> dict[str, Any]:
    keys = (
        "source_language",
        "quality",
        "literal_provider",
        "localization_provider",
        "localization_model",
        "localization_style",
        "text_style",
        "bubble_padding",
        "max_lines",
        "glossary",
        "translation_engine",
        "translation_fallback_engine",
        "translation_memory_enabled",
        "translation_memory_prefer_verified",
        "translate_title",
        "translate_sfx",
        "translate_sign",
        "translate_credit",
        "qwen_model_path",
        "qwen_model_name",
        "provider_models",
        "provider_base_urls",
    )
    return {
        "target_language": target,
        **{key: config.get(key) for key in keys},
    }


def _preprocessing_settings_fingerprint(
    config: dict[str, Any],
    quality: str,
) -> str:
    return _stable_json_hash({
        "kind": "preprocessing-settings-v1",
        "quality": quality,
        "full_page_ocr_max_side": FULL_PAGE_OCR_MAX_SIDE,
        "debug_artifacts": bool(
            config.get("debug_artifacts_enabled", False)
        ),
    })


def _preprocessing_stage_fingerprint(
    source: Path,
    settings_fingerprint: str,
) -> str:
    return _stable_json_hash({
        "kind": "preprocessing-stage-v1",
        "source_sha256": _file_digest(source),
        "settings_fingerprint": settings_fingerprint,
    })


def _ocr_settings_fingerprint(
    requested_source: str,
    quality: str,
    preferred: str | None,
    preprocessing: dict[str, Any],
) -> str:
    return _stable_json_hash({
        "kind": "ocr-settings-v1",
        "requested_source": requested_source,
        "quality": quality,
        "preferred_language": preferred or "",
        "preprocessing": preprocessing,
        "full_page_ocr_max_side": FULL_PAGE_OCR_MAX_SIDE,
    })


def _normalized_dialogue_payload(page: PageDialogue) -> list[dict[str, Any]]:
    normalized = []
    for item in page.dialogue:
        normalized.append({
            "id": str(item.get("id", "")),
            "text": normalize_global_text(str(item.get("text", ""))).strip(),
            "source_text": normalize_global_text(
                str(item.get("source_text", ""))
            ).strip(),
            "region_type": normalize_region_type(item.get("region_type")),
            "reading_order": int(item.get("reading_order", 0) or 0),
        })
    return normalized


def _model_identity_payload(config: dict[str, Any]) -> dict[str, Any]:
    engine = str(
        config.get("translation_engine", "qwen") or "qwen"
    ).strip().casefold()
    provider_models = dict(config.get("provider_models", {}) or {})
    provider_base_urls = dict(config.get("provider_base_urls", {}) or {})
    qwen_path = Path(str(
        config.get("qwen_model_path")
        or config.get("qwen_model")
        or ""
    ))
    qwen_file: dict[str, Any] = {}
    if engine == "qwen" and qwen_path.is_file():
        try:
            stat = qwen_path.stat()
            qwen_file = {
                "path": str(qwen_path.resolve()),
                "size": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
            }
        except OSError:
            qwen_file = {"path": str(qwen_path)}
    return {
        "primary_model": (
            config.get("qwen_model_name", "")
            if engine == "qwen"
            else provider_models.get(engine, "")
        ),
        "fallback_model": provider_models.get(
            str(config.get("translation_fallback_engine", "")).casefold(),
            "",
        ),
        "provider_models": provider_models,
        "provider_base_urls": provider_base_urls,
        "qwen_file": qwen_file,
    }


def _provider_identity(config: dict[str, Any]) -> str:
    return json.dumps({
        "primary": str(
            config.get("translation_engine", "qwen") or "qwen"
        ).strip().casefold(),
        "fallback": str(
            config.get("translation_fallback_engine", "") or ""
        ).strip().casefold(),
        "allow_local_fallback_for_cloud": bool(
            config.get("allow_local_fallback_for_cloud", False)
        ),
        "provider_base_urls": dict(config.get("provider_base_urls", {}) or {}),
    }, sort_keys=True, separators=(",", ":"))


def _model_identity(config: dict[str, Any]) -> str:
    return json.dumps(
        _model_identity_payload(config),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _translation_settings_fingerprint(
    config: dict[str, Any],
    target: str,
) -> str:
    return _stable_json_hash({
        "kind": "translation-settings-v1",
        "target_language": target,
        "glossary": dict(config.get("glossary", {}) or {}),
        "localization_style": config.get("localization_style", "Manga"),
        "literal_provider": config.get("literal_provider", ""),
        "localization_provider": config.get("localization_provider", ""),
        "localization_model": config.get("localization_model", ""),
        "translation_memory_enabled": bool(
            config.get("translation_memory_enabled", True)
        ),
        "translation_memory_prefer_verified": bool(
            config.get("translation_memory_prefer_verified", True)
        ),
        "region_translation_policy": {
            key: bool(config.get(key, True))
            for key in (
                "translate_title",
                "translate_sfx",
                "translate_sign",
                "translate_credit",
            )
        },
        "provider": _provider_identity(config),
        "model": _model_identity_payload(config),
    })


def _translation_stage_fingerprint(
    page: PageDialogue,
    config: dict[str, Any],
    target: str,
) -> str:
    return _stable_json_hash({
        "kind": "translation-stage-v2",
        "source_language": page.source_language,
        "target_language": target,
        "dialogue": _normalized_dialogue_payload(page),
        "page_context": normalize_global_text(page.page_context).strip(),
        "settings_fingerprint": _translation_settings_fingerprint(
            config,
            target,
        ),
    })


def _render_settings_fingerprint(
    config: dict[str, Any],
    target: str,
) -> str:
    return _stable_json_hash({
        "kind": "render-settings-v1",
        "target_language": target,
        "text_style": config.get("text_style", "Manga"),
        "bubble_padding": config.get("bubble_padding", 5),
        "max_lines": config.get("max_lines", 3),
        "auto_fit": config.get("auto_fit", True),
    })


def _project_resume_config(project, settings=None) -> dict[str, Any]:
    """Build the same policy inputs used by PipelineService for recovery."""
    return project_pipeline_config(project, settings)


def _existing_artifacts(**paths: Path) -> dict[str, Path]:
    return {
        name: path
        for name, path in paths.items()
        if path.is_file()
    }


def _completed_project_output(
    project,
    image,
) -> dict[str, str] | None:
    source = Path(image.source_path)
    translation_path = target_translation_path(
        project.artifacts,
        image.id,
        project.target_language,
    )
    render_dir = target_render_dir(
        project.artifacts,
        image.id,
        project.target_language,
    )
    final_path = render_dir / rendered_filename(
        source,
        project.target_language,
    )
    if not translation_path.is_file() or not final_path.is_file():
        return None
    preview_path = render_dir / f"{source.stem}_preview.png"
    try:
        payload = json.loads(translation_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        payload = {}
    groups = payload.get("translation_groups", [])
    review = any(
        isinstance(group, dict) and group.get("status") == "review"
        for group in groups
    ) or int(payload.get("ai_review", {}).get("issue_count", 0) or 0) > 0
    return {
        "status": "review" if review else "ready",
        "translation_result": str(translation_path),
        "rendered_image": str(final_path),
        "preview_image": str(preview_path) if preview_path.is_file() else "",
        "error": "",
    }


def _clear_page_retranslate_artifacts(project, image, paths=None) -> None:
    paths = paths or PATHS
    source = Path(image.source_path)
    target_artifacts = target_root(project.artifacts, project.target_language)
    artifact_paths = [
        project.artifacts / f"{image.id}_ocr.json",
        target_translation_path(
            project.artifacts,
            image.id,
            project.target_language,
        ),
        target_artifacts / f"{image.id}_timing.json",
        target_artifacts / f"{image.id}_intelligent_page.json",
    ]
    for raw in (
        image.ocr_result,
        image.translation_result,
        image.rendered_image,
        image.preview_image,
    ):
        if raw:
            artifact_paths.append(Path(raw))
    for path in artifact_paths:
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
    for render_dir in {
        target_render_dir(project.artifacts, image.id, project.target_language),
        project.artifacts / image.id,
    }:
        try:
            if render_dir.is_dir():
                shutil.rmtree(render_dir)
        except OSError:
            pass
    for cache_root in (paths.ocr_cache, paths.page_translation_cache):
        try:
            for path in cache_root.glob(f"{_cache_fragment(source.stem)}_*.json"):
                try:
                    path.unlink()
                except OSError:
                    pass
        except OSError:
            pass
    image.ocr_result = ""
    image.translation_result = ""
    image.rendered_image = ""
    image.preview_image = ""
    image.error = ""


def _state_manager_for_stages(
    job_manifest: JobManifest,
    *contracts: StageContract,
) -> PipelineStateManager:
    return PipelineStateManager(
        job_manifest,
        graph=PipelineDependencyGraph(contracts),
    )


def _record_stage_completion(
    job_manifest: JobManifest,
    image_id: str,
    stage: str,
    **kwargs,
) -> None:
    PipelineStateManager(job_manifest).record_stage(image_id, stage, **kwargs)


def _box_from_polygon(polygon: list[list[int]]) -> tuple[int, int, int, int]:
    xs = [int(point[0]) for point in polygon]
    ys = [int(point[1]) for point in polygon]
    return min(xs), min(ys), max(xs), max(ys)



