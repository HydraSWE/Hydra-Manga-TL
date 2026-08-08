"""Cancellable background pipeline using the validated OCR/translation/render engines."""

from __future__ import annotations

import json
import gc
import hashlib
import logging
import re
import shutil
import threading
import tempfile
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from PySide6.QtCore import QObject, Qt, Signal, Slot

from hydra_manga_tl.phase.context_engine import ContextEngine
from hydra_manga_tl.phase.intelligent_page import IntelligentPageResult
from hydra_manga_tl.phase.job_manifest import JobManifest
from hydra_manga_tl.core.language import resolve_source_language
from hydra_manga_tl.phase.layout import (
    TextGroup,
    classify_text_group,
    decorative_symbols_from_regions,
    group_regions,
    is_decorative_mark_text,
)
from hydra_manga_tl.phase.layout_graph import build_layout_graph
from hydra_manga_tl.ocr.core import (
    FULL_PAGE_OCR_MAX_SIDE,
    OCRResult,
    clean_ocr_text,
)
from hydra_manga_tl.ocr.service import OCRService, current_rss_mb
from hydra_manga_tl.phase.phase3 import run as render_phase3
from hydra_manga_tl.core.paths import PATHS
from hydra_manga_tl.phase.preprocessor import prepare_ocr_image
from hydra_manga_tl.phase.review import review_translation_groups
from hydra_manga_tl.phase.segmentation import segment_bubble
from hydra_manga_tl.core.settings import SETTINGS
from hydra_manga_tl.phase.stage_streaming import BoundedStageExecutor
from hydra_manga_tl.phase.state_manager import (
    PipelineDependencyGraph,
    PipelineStateManager,
    StageAction,
    StageContract,
    StageValidationRequest,
)
from hydra_manga_tl.title import detect_title_objects
from hydra_manga_tl.phase.typesetting import review_rendered_group, summarize_render_review
from hydra_manga_tl.phase.render_queue import RENDER_QUEUE
from hydra_manga_tl.translation.engines import PageDialogue, PageTranslation
from hydra_manga_tl.translation.requests import RenderRequest
from hydra_manga_tl.translation.requests import TranslationRequest, TranslationRequestStatus, TranslationRequestType
from hydra_manga_tl.translation.cache_store import TRANSLATION_CACHE
from hydra_manga_tl.translation.queue import CancellationToken, RequestCancelled, TRANSLATION_QUEUE, TranslationQueue
from hydra_manga_tl.translation.runtime import TRANSLATION_RUNTIME
from hydra_manga_tl.translation.memory import (
    learn_validated_page,
    source_region_hash,
    source_text_hash,
)
from hydra_manga_tl.translation.scheduler import (
    ParallelPageJob,
    ParallelPageScheduler,
    PageTranslationOutcome,
    ProviderDispatcher,
    SchedulerSnapshot,
    SmartPageJob,
    SmartTranslationScheduler,
    resolve_worker_count,
    timed_stage,
)
from hydra_manga_tl.core.normalization import normalize_global_text
from hydra_manga_tl.core.region_types import normalize_region_type
from hydra_manga_tl.project.artifacts import (
    rendered_filename,
    target_manifest_path,
    target_render_dir,
    target_root,
    target_translation_path,
)
from hydra_manga_tl import __version__


LOGGER = logging.getLogger(__name__)


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


def _page_translation_cache_key(page: PageDialogue, config: dict[str, Any], target: str) -> str:
    return TRANSLATION_CACHE.page_translation_key(page, config, target)


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


def _project_resume_config(project) -> dict[str, Any]:
    """Build the same policy inputs used by PipelineService for recovery."""
    return {
        "source_language": project.source_language,
        "quality": project.quality,
        "literal_provider": project.literal_provider,
        "localization_provider": project.localization_provider,
        "localization_model": project.localization_model,
        "localization_style": project.localization_style,
        "text_style": project.text_style,
        "auto_fit": project.auto_fit,
        "bubble_padding": project.bubble_padding,
        "max_lines": project.max_lines,
        "glossary": project.glossary,
        "translation_engine": SETTINGS.translation_engine,
        "translation_fallback_engine": SETTINGS.translation_fallback_engine,
        "translation_memory_enabled": SETTINGS.translation_memory_enabled,
        "translation_memory_prefer_verified": (
            SETTINGS.translation_memory_prefer_verified
        ),
        "translate_title": SETTINGS.translate_titles,
        "translate_sfx": SETTINGS.translate_sfx,
        "translate_sign": SETTINGS.translate_signs,
        "translate_credit": SETTINGS.translate_credits,
        "qwen_model_path": SETTINGS.qwen_model_path,
        "qwen_model_name": SETTINGS.qwen_model_name,
        "provider_models": {
            "groq": SETTINGS.groq_model,
            "gemini": SETTINGS.gemini_model,
            "deepseek": SETTINGS.deepseek_model,
            "openai": SETTINGS.openai_model,
            "openai_compatible": SETTINGS.openai_compatible_model,
        },
        "provider_base_urls": {
            "openai_compatible": SETTINGS.openai_compatible_base_url,
        },
    }


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


def _clear_page_retranslate_artifacts(project, image) -> None:
    source = Path(image.source_path)
    target_artifacts = target_root(project.artifacts, project.target_language)
    paths = [
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
            paths.append(Path(raw))
    for path in paths:
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
    for cache_root in (PATHS.ocr_cache, PATHS.page_translation_cache):
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


class PipelineWorker(QObject):
    stage = Signal(str, str, int, int, str)
    image_finished = Signal(str, object)
    image_failed = Signal(str, str)
    finished = Signal(bool)
    scheduler_snapshot = Signal(object)

    def __init__(self, items: list[dict], artifacts: Path, target: str, cancel: threading.Event, config: dict | None = None) -> None:
        super().__init__()
        self.items = items
        self.artifacts = artifacts
        self.target = target
        self.cancel = cancel
        self.config = config or {}
        self.target_artifacts = target_root(self.artifacts, self.target)

    def _run_ocr_page(
        self,
        ocr_service: OCRService,
        source_for_ocr: Path,
        *,
        preferred_language: str | None,
        quality: str,
        auto_language_fallback: bool,
        cache_path: Path,
        checkpoint_path: Path,
    ):
        return ocr_service.analyze_page(
            source_for_ocr,
            preferred_language=preferred_language,
            quality=quality,
            auto_language_fallback=auto_language_fallback,
            cache_path=cache_path,
            checkpoint_path=checkpoint_path,
        )

    @staticmethod
    def _build_page_dialogue(
        source_language: str,
        target_language: str,
        dialogue: list[dict[str, Any]],
        page_context: str,
    ) -> PageDialogue:
        return PageDialogue(
            source_language=source_language,
            target_language=target_language,
            dialogue=dialogue,
            page_context=page_context,
        )

    def _translate_page_dialogue(self, page: PageDialogue) -> PageTranslation:
        if self.cancel.is_set():
            raise RuntimeError("Translation request was cancelled")
        provider = str(self.config.get("translation_engine", "qwen") or "qwen").strip().lower()
        if provider == "openai_compatible":
            provider_models = dict(self.config.get("provider_models", {}) or {})
            provider_base_urls = dict(self.config.get("provider_base_urls", {}) or {})
            LOGGER.info(
                "OpenAI-compatible pipeline translation started units=%d base_url=%s model=%s",
                len(page.dialogue),
                provider_base_urls.get("openai_compatible", ""),
                provider_models.get("openai_compatible", ""),
            )
            try:
                result = TRANSLATION_RUNTIME.translate_page(page, self.config)
            except Exception as error:
                LOGGER.warning(
                    "OpenAI-compatible pipeline translation failed units=%d error=%s",
                    len(page.dialogue),
                    error,
                )
                raise
            LOGGER.info(
                "OpenAI-compatible pipeline translation finished units=%d returned=%d provider=%s",
                len(page.dialogue),
                len(result.translations),
                TRANSLATION_RUNTIME.last_engine_id,
            )
            return result
        return TRANSLATION_RUNTIME.translate_page(page, self.config)

    def _resume_verified_render(
        self,
        item: dict[str, Any],
        position: int,
        total: int,
        job_manifest: JobManifest,
    ) -> bool:
        """Emit a completed page only when every persisted artifact still matches."""
        if bool(self.config.get("force_retranslate", False)):
            return False

        image_id = str(item["id"])
        source = Path(item["source_path"])
        translation_path = target_translation_path(
            self.artifacts,
            image_id,
            self.target,
        )
        render_dir = target_render_dir(self.artifacts, image_id, self.target)
        final_path = render_dir / rendered_filename(source, self.target)
        preview_path = render_dir / f"{source.stem}_preview.png"
        render_report_path = render_dir / f"{source.stem}_render.json"

        try:
            translation_record = (
                job_manifest.pages.get(image_id).stage_records.get(
                    "translating",
                    {},
                )
                if job_manifest.pages.get(image_id) is not None
                else {}
            )
            translation_input_fingerprint = str(
                translation_record.get("input_fingerprint", "")
            )
            ocr_path = self.artifacts / f"{image_id}_ocr.json"
            render_input_fingerprint = _render_stage_fingerprint(
                source,
                translation_path,
                target=self.target,
                config=self.config,
            )
            expected_render_artifacts = _existing_artifacts(
                rendered_image=final_path,
                preview_image=preview_path,
                render_report=render_report_path,
            )
            state_manager = _state_manager_for_stages(
                job_manifest,
                StageContract("translating"),
                StageContract("rendering", requires=("translating",)),
            )
            plan = state_manager.plan_page(
                image_id,
                source,
                {
                    "translating": StageValidationRequest(
                        input_fingerprint=translation_input_fingerprint,
                        artifacts={"translation_result": translation_path},
                        input_artifacts={"ocr_result": ocr_path},
                        source_path=source,
                        application_version=__version__,
                        settings_fingerprint=_translation_settings_fingerprint(
                            self.config,
                            self.target,
                        ),
                        provider_identity=_provider_identity(self.config),
                        model_identity=_model_identity(self.config),
                    ),
                    "rendering": StageValidationRequest(
                        input_fingerprint=render_input_fingerprint,
                        artifacts=expected_render_artifacts,
                        input_artifacts={
                            "translation_result": translation_path,
                        },
                        source_path=source,
                        application_version=__version__,
                        settings_fingerprint=_render_settings_fingerprint(
                            self.config,
                            self.target,
                        ),
                    ),
                },
            )
            if (
                plan.action_for("translating") is not StageAction.SKIP
                or plan.action_for("rendering") is not StageAction.SKIP
            ):
                return False
            payload = json.loads(translation_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, KeyError):
            return False

        groups = list(payload.get("translation_groups", []))
        ai_review = dict(payload.get("ai_review") or {})
        needs_review = (
            any(str(group.get("status", "")) == "review" for group in groups)
            or int(ai_review.get("issue_count", 0) or 0) > 0
        )
        job_manifest.mark(image_id, "done", stage="review")
        self.stage.emit(
            image_id,
            "rendering",
            position,
            total,
            f"Reusing verified output for {source.name}",
        )
        self.image_finished.emit(image_id, {
            "status": "review" if needs_review else "ready",
            "source_language": str(payload.get("source_language", "")),
            "ocr_result": str(ocr_path) if ocr_path.is_file() else "",
            "translation_result": str(translation_path),
            "rendered_image": str(final_path),
            "preview_image": str(preview_path) if preview_path.is_file() else "",
            "error": "",
        })
        return True

    def _resume_verified_translation(
        self,
        item: dict[str, Any],
        position: int,
        total: int,
        job_manifest: JobManifest,
        *,
        translation_input_fingerprint: str,
    ) -> bool:
        """Reuse a verified translation and rebuild only downstream output."""
        if bool(self.config.get("force_retranslate", False)):
            return False
        image_id = str(item["id"])
        source = Path(item["source_path"])
        ocr_path = self.artifacts / f"{image_id}_ocr.json"
        translation_path = target_translation_path(
            self.artifacts,
            image_id,
            self.target,
        )
        state_manager = _state_manager_for_stages(
            job_manifest,
            StageContract("translating"),
        )
        plan = state_manager.plan_page(
            image_id,
            source,
            {
                "translating": StageValidationRequest(
                    input_fingerprint=translation_input_fingerprint,
                    artifacts={"translation_result": translation_path},
                    input_artifacts={"ocr_result": ocr_path},
                    source_path=source,
                    application_version=__version__,
                    settings_fingerprint=_translation_settings_fingerprint(
                        self.config,
                        self.target,
                    ),
                    provider_identity=_provider_identity(self.config),
                    model_identity=_model_identity(self.config),
                ),
            },
        )
        if plan.action_for("translating") is not StageAction.SKIP:
            return False

        started = time.perf_counter()
        payload = json.loads(translation_path.read_text(encoding="utf-8"))
        translated_groups = list(payload.get("translation_groups", []))
        render_input_fingerprint = _render_stage_fingerprint(
            source,
            translation_path,
            target=self.target,
            config=self.config,
        )
        job_manifest.mark(image_id, "rendering", stage="translating")
        self.stage.emit(
            image_id,
            "reconstructing",
            position,
            total,
            f"Reusing verified translation for {source.name}",
        )
        render_dir = target_render_dir(
            self.artifacts,
            image_id,
            self.target,
        )
        render_request = RenderRequest(
            request_id=f"batch:{image_id}",
            project_id=str(self.config.get("project_id") or ""),
            image_id=image_id,
            image_index=int(item.get("image_index", position - 1)),
            result_path=translation_path,
            render_dir=render_dir,
            source_path=source,
            reason="batch",
        )
        render_report = self._render_translation_payload(render_request)
        render_json = render_dir / f"{source.stem}_render.json"
        render_details = (
            json.loads(render_json.read_text(encoding="utf-8"))
            if render_json.is_file()
            else {}
        )
        rendered_by_group = {
            int(value.get("group", 0)): value
            for value in render_details.get("rendered_groups", [])
        }
        with Image.open(source) as opened:
            page_size = opened.size
        typeset_reviews = [
            review_rendered_group(
                group,
                rendered_by_group.get(
                    int(group.get("index", 0) or 0),
                    {},
                ),
                page_size,
            )
            for group in translated_groups
            if group.get("status") in {"translated", "review"}
        ]
        render_review = summarize_render_review(typeset_reviews)
        ai_review = review_translation_groups(
            translated_groups,
            render_review,
        )
        payload["render_review"] = render_review
        payload["ai_review"] = ai_review
        _write_json_atomic(translation_path, payload)
        provider_identity = _provider_identity(self.config)
        model_identity = _model_identity(self.config)
        translation_settings = _translation_settings_fingerprint(
            self.config,
            self.target,
        )
        _record_stage_completion(job_manifest,
            image_id,
            "translating",
            input_fingerprint=translation_input_fingerprint,
            input_artifacts={"ocr_result": ocr_path},
            artifacts={"translation_result": translation_path},
            source_path=source,
            application_version=__version__,
            settings_fingerprint=translation_settings,
            provider_identity=provider_identity,
            model_identity=model_identity,
            metadata={
                "target_language": self.target,
                "reused": True,
                "providers_used": sorted({
                    str(group.get("provider", ""))
                    for group in translated_groups
                    if str(group.get("provider", ""))
                }),
            },
        )
        final_path = render_dir / rendered_filename(source, self.target)
        preview_path = render_dir / f"{source.stem}_preview.png"
        render_outputs = _existing_artifacts(
            rendered_image=final_path,
            preview_image=preview_path,
            render_report=render_json,
        )
        _record_stage_completion(job_manifest,
            image_id,
            "rendering",
            input_fingerprint=render_input_fingerprint,
            input_artifacts={
                "translation_result": translation_path,
            },
            artifacts=render_outputs,
            source_path=source,
            application_version=__version__,
            settings_fingerprint=_render_settings_fingerprint(
                self.config,
                self.target,
            ),
            metadata={
                "target_language": self.target,
                "translation_reused": True,
            },
        )
        intelligent_path = (
            self.target_artifacts / f"{image_id}_intelligent_page.json"
        )
        timing = {
            "image_id": image_id,
            "source": str(source),
            "position": position,
            "total": total,
            "quality": self.config.get("quality", "Balanced"),
            "translation_checkpoint_reused": True,
            "stages": {
                "render_seconds": round(time.perf_counter() - started, 3),
            },
        }
        intelligent = IntelligentPageResult(
            pipeline_version=__version__,
            image_id=image_id,
            source=str(source.resolve()),
            target_language=self.target,
            preprocessing=dict(payload.get("preprocessing", {})),
            ocr_attempts=dict(payload.get("ocr_attempts", {})),
            layout_graph=dict(payload.get("layout_graph", {})),
            bubble_segmentation=list(
                payload.get("bubble_segmentation", [])
            ),
            translation_units=list(payload.get("translation_units", [])),
            render_review={
                "typesetting": render_review,
                "ai_review": ai_review,
                "render_report": render_report,
            },
            debug_artifacts=list(payload.get("debug_artifacts", [])),
            timing=timing,
        )
        _write_json_atomic(intelligent_path, intelligent.to_dict())
        review_input_fingerprint = _stable_json_hash({
            "kind": "review-stage-v1",
            "render_input_fingerprint": render_input_fingerprint,
            "render_review": render_review,
            "ai_review": ai_review,
        })
        _record_stage_completion(job_manifest,
            image_id,
            "review",
            input_fingerprint=review_input_fingerprint,
            input_artifacts=render_outputs,
            artifacts={
                "reviewed_translation": translation_path,
                "intelligent_page": intelligent_path,
            },
            source_path=source,
            application_version=__version__,
            settings_fingerprint=_render_settings_fingerprint(
                self.config,
                self.target,
            ),
            metadata={"target_language": self.target},
        )
        self._write_image_timing(image_id, timing)
        needs_review = (
            any(
                str(group.get("status", "")) == "review"
                for group in translated_groups
            )
            or int(ai_review.get("issue_count", 0) or 0) > 0
        )
        job_manifest.mark(image_id, "done", stage="review")
        self.image_finished.emit(image_id, {
            "status": "review" if needs_review else "ready",
            "source_language": str(payload.get("source_language", "")),
            "ocr_result": str(ocr_path),
            "translation_result": str(translation_path),
            "rendered_image": str(final_path),
            "preview_image": (
                str(preview_path) if preview_path.is_file() else ""
            ),
            "error": "",
        })
        return True

    def _build_translation_payload(
        self,
        *,
        source: Path,
        source_language: str,
        ocr_result: OCRResult,
        source_regions: list[dict[str, Any]],
        translated_groups: list[dict[str, Any]],
        preprocessed: Any,
        layout_graph: dict[str, Any],
        bubble_segmentations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "pipeline_version": __version__,
            "project_id": str(self.config.get("project_id") or ""),
            "source": str(source.resolve()),
            "source_language": source_language,
            "target_language": self.target,
            "ocr_model_language": ocr_result.model_language,
            "source_regions": source_regions,
            "translation_groups": translated_groups,
            "preprocessing": (
                preprocessed.to_dict() if hasattr(preprocessed, "to_dict")
                else dict(preprocessed)
            ),
            "ocr_attempts": ocr_result.metadata.get("manager", {}),
            "layout_graph": layout_graph,
            "bubble_segmentation": bubble_segmentations,
            "translation_units": layout_graph["translation_units"],
            "literal_provider": self.config.get("literal_provider", "marian"),
            "localization_provider": self.config.get("localization_provider", "local"),
            "localization_style": self.config.get("localization_style", "Manga"),
            "text_style": self.config.get("text_style", "Manga"),
            "bubble_padding": int(self.config.get("bubble_padding", 5)),
            "max_lines": int(self.config.get("max_lines", 3)),
        }

    def _learn_translation_groups(
        self,
        page: PageDialogue,
        page_result: PageTranslation,
        translated_groups: list[dict[str, Any]],
    ) -> int:
        if not (
            self.config.get("translation_memory_enabled", True)
            and self.config.get("translation_memory_auto_learn", True)
        ):
            return 0
        resolved_by_id = {
            str(item.get("id", "")): dict(item)
            for item in page_result.translations
        }
        valid_ids: list[str] = []
        final_translations: list[dict[str, Any]] = []
        for group in translated_groups:
            entry_id = f"r{int(group.get('index', 0) or 0)}"
            if entry_id not in resolved_by_id:
                continue
            detail = resolved_by_id[entry_id]
            detail["text"] = str(group.get("translated_text", ""))
            final_translations.append(detail)
            if (
                group.get("status") == "translated"
                and not group.get("review_reasons")
                and str(group.get("translated_text", "")).strip()
            ):
                valid_ids.append(entry_id)
        if not valid_ids:
            return 0
        return learn_validated_page(
            page,
            PageTranslation(
                page.source_language,
                page.target_language,
                final_translations,
            ),
            valid_ids=valid_ids,
            project_id=str(self.config.get("project_id") or "") or None,
        )

    @staticmethod
    def _render_translation_payload(request: RenderRequest):
        return RENDER_QUEUE.submit(
            request,
            lambda result_path, output_dir, policy: render_phase3(
                result_path,
                output_dir,
                policy=policy,
                skip_title_reconstruction=(request.reason == "batch"),
            ),
        ).result()["output"]

    def _prepare_fast_page(
        self,
        item: dict[str, Any],
        position: int,
        total: int,
        ocr_service: OCRService,
        requested_source: str,
        preferred: str | None,
        job_manifest: JobManifest,
    ) -> dict[str, Any]:
        image_id, source = str(item["id"]), Path(item["source_path"])
        image_started = time.perf_counter()
        timing: dict[str, Any] = {
            "image_id": image_id,
            "source": str(source),
            "position": position,
            "total": total,
            "source_language_request": requested_source,
            "quality": "Fast",
            "stages": {},
            "counts": {},
            "cache": {"ocr_hit": False, "translation_hit": False},
        }
        job_manifest.ensure_page(image_id, str(source))
        job_manifest.mark(image_id, "preprocessing")
        self.stage.emit(image_id, "preprocessing", position, total, f"Preparing {source.name}")
        started = time.perf_counter()
        ocr_path = self.artifacts / f"{image_id}_ocr.json"
        preprocessed = prepare_ocr_image(source, self.artifacts / image_id / "preprocess")
        timing["stages"]["preprocess_seconds"] = round(time.perf_counter() - started, 3)
        preprocessing_settings = _preprocessing_settings_fingerprint(
            self.config,
            "Fast",
        )
        _record_stage_completion(job_manifest,
            image_id,
            "preprocessing",
            input_fingerprint=_preprocessing_stage_fingerprint(
                source,
                preprocessing_settings,
            ),
            input_artifacts={"source_image": source},
            artifacts={
                "preprocessed_image": Path(preprocessed.ocr_path),
            },
            source_path=source,
            application_version=__version__,
            settings_fingerprint=preprocessing_settings,
            metadata={"quality": "Fast"},
        )
        if self.cancel.is_set():
            raise RequestCancelled("Request cancelled")

        started = time.perf_counter()
        ocr_cache_key = _stable_json_hash({
            "kind": "ocr-v3-preprocessed",
            "base": _ocr_cache_key(source, requested_source, "Fast", preferred),
            "preprocessing": preprocessed.quality.to_dict(),
        })
        ocr_cache_path = PATHS.ocr_cache / f"{_cache_fragment(source.stem)}_{ocr_cache_key[:16]}.json"
        ocr_settings = _ocr_settings_fingerprint(
            requested_source,
            "Fast",
            preferred,
            preprocessed.quality.to_dict(),
        )
        ocr_plan = _state_manager_for_stages(
            job_manifest,
            StageContract("OCR"),
        ).plan_page(
            image_id,
            source,
            {
                "OCR": StageValidationRequest(
                    input_fingerprint=ocr_cache_key,
                    artifacts={"ocr_result": ocr_path},
                    input_artifacts={
                        "preprocessed_image": Path(preprocessed.ocr_path),
                    },
                    source_path=source,
                    application_version=__version__,
                    settings_fingerprint=ocr_settings,
                ),
            },
        )
        force_retranslate = bool(self.config.get("force_retranslate", False))
        verified_checkpoint = (
            ocr_plan.action_for("OCR") is StageAction.SKIP
            and not force_retranslate
        )
        if not verified_checkpoint:
            job_manifest.mark(image_id, "OCR", stage="preprocessing")
            self.stage.emit(
                image_id,
                "ocr",
                position,
                total,
                f"Reading text in {source.name}",
            )
        service_result = self._run_ocr_page(
            ocr_service,
            Path(preprocessed.ocr_path),
            preferred_language=preferred,
            quality="Fast",
            auto_language_fallback=False,
            cache_path=None if force_retranslate else ocr_cache_path,
            checkpoint_path=ocr_path if verified_checkpoint else None,
        )
        ocr_result = service_result.ocr_result
        source_regions = service_result.final_regions
        source_language = resolve_source_language(requested_source, ocr_result.language)
        timing["cache"]["ocr_hit"] = service_result.cache_hit
        timing["cache"]["ocr_checkpoint_verified"] = verified_checkpoint
        timing["ocr_service"] = service_result.telemetry
        timing["stages"]["ocr_seconds"] = round(time.perf_counter() - started, 3)
        _write_json_atomic(ocr_path, ocr_result.to_dict())
        _record_stage_completion(job_manifest,
            image_id,
            "OCR",
            input_fingerprint=ocr_cache_key,
            artifacts={"ocr_result": ocr_path},
            input_artifacts={
                "preprocessed_image": Path(preprocessed.ocr_path),
            },
            source_path=source,
            application_version=__version__,
            settings_fingerprint=ocr_settings,
            metadata={
                "quality": "Fast",
                "source_language_request": requested_source,
                "preferred_language": preferred or "",
            },
        )

        with Image.open(source) as opened:
            page_size = opened.size
        layout_graph = build_layout_graph(source_regions, page_size=page_size)
        groups = group_regions(source_regions)
        with Image.open(source) as opened:
            with opened.convert("RGB") as source_image:
                group_visuals = []
                for group in groups:
                    effective_group, text_member_indices, decorative_symbols = _text_group_without_preserved_marks(
                        group,
                        source_regions,
                    )
                    polygons = [
                        source_regions[index - 1]["polygon"]
                        for index in text_member_indices
                    ]
                    cleanup_polygons = [
                        *polygons,
                        *[
                            symbol.get("polygon")
                            for symbol in decorative_symbols
                            if isinstance(symbol, dict) and symbol.get("polygon")
                        ],
                    ]
                    group_visuals.append((
                        effective_group,
                        polygons,
                        cleanup_polygons,
                        [
                            _source_text_color(source_image, polygon)
                            for polygon in polygons
                        ],
                        source_region_hash(source_image, polygons),
                        decorative_symbols,
                    ))
        group_payloads: list[dict[str, Any]] = []
        group_source_polygons: list[list[list[list[int]]]] = []
        dialogue: list[dict[str, Any]] = []
        for idx, group in enumerate(groups):
            stable_bubble_id = _stable_bubble_id(
                str(self.config.get("project_id") or ""),
                image_id,
                idx,
            )
            display_bubble_id = _bubble_display_id(position, idx)
            placement_bbox = group.bbox
            effective_group, polygons, cleanup_polygons, source_text_colors, region_hash, decorative_symbols = group_visuals[idx]
            placement_bbox = effective_group.bbox
            confidence = min(source_regions[index - 1]["confidence"] for index in effective_group.member_indices)
            bubble_type = _classify_bubble(effective_group)
            classification = classify_text_group(effective_group, source_regions, ocr_result.model_language)
            if bubble_type not in {"credit", "sfx"}:
                bubble_type = classification.kind
            candidate = {
                "type": bubble_type,
                "text": effective_group.text,
                "original_text": effective_group.text,
                "polygon": [
                    [placement_bbox[0], placement_bbox[1]], [placement_bbox[2], placement_bbox[1]],
                    [placement_bbox[2], placement_bbox[3]], [placement_bbox[0], placement_bbox[3]],
                ],
                "source_direction": group.direction,
                "source_polygons": polygons,
                "cleanup_polygons": cleanup_polygons,
                "source_text_colors": source_text_colors,
                "decorative_symbols": decorative_symbols,
                "preserved_marks": [],
            }
            if bubble_type not in {"credit", "sfx", "sign"} and detect_title_objects([candidate], page_size):
                bubble_type = "title"
            payload = {
                "type": bubble_type,
                "text": effective_group.text,
                "confidence": confidence,
                "classification_reasons": classification.reasons,
                "ocr_review_reasons": list(dict.fromkeys(
                    str(reason)
                    for member_index in group.member_indices
                    for reason in source_regions[member_index - 1].get("ocr_review_reasons", [])
                    if str(reason)
                )),
                "polygon": candidate["polygon"],
                "source_text_colors": source_text_colors,
                "cleanup_polygons": cleanup_polygons,
                "decorative_symbols": decorative_symbols,
                "preserved_marks": [],
                "source_text_hash": source_text_hash(effective_group.text),
                "source_region_hash": region_hash,
                "bubble_id": stable_bubble_id,
                "display_id": display_bubble_id,
            }
            group_payloads.append(payload)
            group_source_polygons.append(polygons)
            region_type = normalize_region_type(bubble_type)
            if _auto_translate_region_type(region_type, self.config):
                dialogue.append({
                    "id": f"r{idx + 1}",
                    "bubble_id": stable_bubble_id,
                    "display_id": display_bubble_id,
                    "text": effective_group.text,
                    "decorative_symbols": decorative_symbols,
                    "preserved_marks": [],
                    "confidence": confidence,
                    "source_text": str(effective_group.text).strip(),
                    "reading_order": len(dialogue) + 1,
                    "source_direction": group.direction,
                    "bbox": payload["polygon"],
                    "region_type": region_type,
                    "source_text_hash": payload["source_text_hash"],
                    "source_region_hash": payload["source_region_hash"],
                })
        timing["counts"] = {
            "source_regions": len(source_regions),
            "groups": len(groups),
            "layout_edges": len(layout_graph.edges),
        }
        debug_artifacts = (
            self._write_ocr_debug_artifacts(source, image_id, source_regions, groups, layout_graph.to_dict())
            if bool(self.config.get("debug_artifacts_enabled", False))
            else {}
        )
        bubble_segmentations = self._segment_groups(source, image_id, groups, group_payloads)
        prepared = {
            "item": item,
            "position": position,
            "source": str(source),
            "source_language": source_language,
            "ocr_path": str(ocr_path),
            "ocr_result": ocr_result.to_dict(),
            "source_regions": source_regions,
            "preprocessed": preprocessed.to_dict(),
            "page_size": list(page_size),
            "layout_graph": layout_graph.to_dict(),
            "groups": [asdict(item[0]) for item in group_visuals],
            "group_payloads": group_payloads,
            "group_source_polygons": group_source_polygons,
            "bubble_segmentations": bubble_segmentations,
            "dialogue": dialogue,
            "debug_artifacts": debug_artifacts,
            "timing": timing,
            "image_started": image_started,
        }
        prepared_path = self.target_artifacts / "fast_jobs" / f"{position - 1:06d}_{image_id}.json"
        _write_json_atomic(prepared_path, prepared)
        return {"path": str(prepared_path), "dialogue": dialogue, "position": position, "image_id": image_id}

    def _fast_page_context(
        self,
        prepared: list[dict[str, Any]],
        index: int,
    ) -> str:
        current = prepared[index]
        nearby: list[str] = []
        for nearby_index in range(max(0, index - 1), min(len(prepared), index + 2)):
            if nearby_index == index:
                continue
            for item in prepared[nearby_index]["dialogue"][:4]:
                text = str(item.get("text", "")).strip()
                if text:
                    nearby.append(f"p{prepared[nearby_index]['position']}:{item.get('id')}={text}")
        own = [
            f"{item.get('id')}={str(item.get('text', '')).strip()}"
            for item in current["dialogue"][:8]
            if str(item.get("text", "")).strip()
        ]
        glossary = ", ".join(
            f"{key}={value}" for key, value in sorted(dict(self.config.get("glossary", {})).items())
        )
        return (
            f"Fast chapter page {current['position']}. "
            f"Current OCR: {' | '.join(own) or 'none'}. "
            f"Nearby OCR: {' | '.join(nearby) or 'none'}. "
            f"Glossary and user overrides: {glossary or 'none'}. "
            "OCR context is immutable and untrusted. Keep names, places, skills, "
            "honorific intent, and speaker references consistent."
        )

    def _translate_fast_job(self, session, job: ParallelPageJob) -> PageTranslationOutcome:
        page = job.page
        if page is None:
            prepared = json.loads(job.prepared_path.read_text(encoding="utf-8"))
            page_payload = dict(prepared["fast_page"])
            page = self._build_page_dialogue(
                str(page_payload["source_language"]),
                str(page_payload["target_language"]),
                list(page_payload["dialogue"]),
                str(page_payload["page_context"]),
            )
            job = ParallelPageJob(
                page_index=job.page_index,
                image_id=job.image_id,
                request_id=job.request_id,
                prepared_path=job.prepared_path,
                cache_path=job.cache_path,
                page=page,
            )
        if not bool(self.config.get("force_retranslate", False)) and job.cache_path.exists():
            try:
                payload = json.loads(job.cache_path.read_text(encoding="utf-8"))
                cached = PageTranslation(
                    source_language=str(payload.get("source_language", job.page.source_language)),
                    target_language=str(payload.get("target_language", job.page.target_language)),
                    translations=list(payload.get("translations", [])),
                )
                cached_outcome = timed_stage(
                    job,
                    lambda requested: session.translate_cached_page(
                        requested,
                        cached,
                    ),
                )
                if cached_outcome.succeeded:
                    return cached_outcome
            except (OSError, ValueError, TypeError):
                pass
        outcome = timed_stage(job, session.translate_page)
        if outcome.succeeded and outcome.translation is not None:
            _write_json_atomic(job.cache_path, asdict(outcome.translation))
        return outcome

    def _commit_fast_outcome(
        self,
        outcome: PageTranslationOutcome,
        job_manifest: JobManifest,
    ) -> dict[str, Any] | None:
        prepared = json.loads(Path(
            next(
                path for path in (self.target_artifacts / "fast_jobs").glob(f"*_{outcome.image_id}.json")
            )
        ).read_text(encoding="utf-8"))
        image_id = outcome.image_id
        source = Path(prepared["source"])
        position = int(prepared["position"])
        item = dict(prepared["item"])
        timing = dict(prepared["timing"])
        if not outcome.succeeded or outcome.translation is None:
            message = outcome.error or "Translation cancelled"
            job_manifest.mark(image_id, "failed", error=message)
            timing["error"] = message
            timing["stages"]["translate_seconds"] = round(outcome.elapsed_seconds, 3)
            timing["total_seconds"] = round(time.perf_counter() - float(prepared["image_started"]), 3)
            self._write_image_timing(image_id, timing)
            self.image_failed.emit(image_id, message)
            return timing

        page_result = outcome.translation
        timing["stages"]["translate_seconds"] = round(outcome.elapsed_seconds, 3)
        timing["translation_attempts"] = outcome.attempts
        timing["cache"]["translation_hit"] = outcome.provider_id == "page-cache"
        source_regions = list(prepared["source_regions"])
        groups = [TextGroup(**group) for group in prepared["groups"]]
        group_payloads = list(prepared["group_payloads"])
        group_source_polygons = list(prepared["group_source_polygons"])
        bubble_segmentations = list(prepared["bubble_segmentations"])
        translated_map = {
            str(item.get("id")): normalize_global_text(str(item.get("text", "")))
            for item in page_result.translations
        }
        translated_details = {
            str(item.get("id")): dict(item)
            for item in page_result.translations
        }
        for key, value in tuple(translated_map.items()):
            stripped = value.strip().rstrip(".!?")
            half = len(stripped) // 2
            if half >= 4:
                first = stripped[:half].strip().rstrip(".,!?;: ")
                second = stripped[half:].strip().rstrip(".,!?;: ")
                if first == second or second.startswith(first) or first.startswith(second):
                    translated_map[key] = first.rstrip(".,!?;: ") + "!"
        translated_groups: list[dict[str, Any]] = []
        for idx, (group, polygons, payload, segmentation_payload) in enumerate(zip(
            groups, group_source_polygons, group_payloads, bubble_segmentations,
        )):
            region_index = idx + 1
            bubble_id = f"r{region_index}"
            region_type = normalize_region_type(payload.get("type"))
            if not _auto_translate_region_type(region_type, self.config):
                translated_text = ""
                status = "preserved"
                review_reasons = list(payload.get("classification_reasons", [])) or [
                    f"{region_type}_translation_disabled"
                ]
            else:
                translated_text = normalize_global_text(translated_map.get(bubble_id, ""))
                review_reasons = list(payload.get("classification_reasons", []))
                review_reasons.extend(f"ocr:{reason}" for reason in payload.get("ocr_review_reasons", []))
                if not translated_text or translated_text == group.text:
                    review_reasons.append("low_confidence_or_empty")
                status = "review" if review_reasons else "translated"
            translated_groups.append({
                "index": region_index,
                "bubble_id": str(payload.get("bubble_id", "")),
                "display_id": str(payload.get("display_id", "")),
                "original_text": str(group.text).strip(),
                "literal_text": str(group.text).strip(),
                "translated_text": translated_text,
                "type": region_type,
                "bubble_type": region_type,
                "ocr_confidence": float(min(
                    source_regions[index - 1]["confidence"] for index in group.member_indices
                )),
                "polygon": payload.get("polygon"),
                "status": status,
                "review_reasons": list(dict.fromkeys(review_reasons)),
                "alternatives": [],
                "provider": str(
                    translated_details.get(bubble_id, {}).get("provider_id")
                    or outcome.provider_id
                ),
                "translation_source": str(
                    translated_details.get(bubble_id, {}).get(
                        "translation_source",
                        "provider",
                    )
                ),
                "tm_entry_id": translated_details.get(bubble_id, {}).get(
                    "tm_entry_id"
                ),
                "source_text_hash": payload.get("source_text_hash", ""),
                "source_region_hash": payload.get("source_region_hash"),
                "model": "",
                "localization_style": self.config.get("localization_style", "Manga"),
                "translation_quality": "good" if status == "translated" else "review",
                "localization_note": "",
                "member_region_indices": group.member_indices,
                "source_direction": group.direction,
                "direction": "horizontal-ltr",
                "source_polygons": polygons,
                "cleanup_polygons": list(payload.get("cleanup_polygons", [])),
                "decorative_symbols": list(payload.get("decorative_symbols", [])),
                "preserved_marks": list(payload.get("preserved_marks", [])),
                "source_member_texts": [
                    str(source_regions[index - 1].get("text", "")) for index in group.member_indices
                ],
                "source_text_colors": list(payload.get("source_text_colors", [])),
                "bubble_segmentation": segmentation_payload,
                "bubble_mask": str(segmentation_payload.get("mask_path", "")),
                "safe_area": list(segmentation_payload.get("safe_area", [])),
            })

        ocr_result = OCRResult.from_dict(prepared["ocr_result"])
        phase2 = self._build_translation_payload(
            source=source,
            source_language=str(prepared["source_language"]),
            ocr_result=ocr_result,
            source_regions=source_regions,
            translated_groups=translated_groups,
            preprocessed=prepared["preprocessed"],
            layout_graph=prepared["layout_graph"],
            bubble_segmentations=bubble_segmentations,
        )
        translation_path = target_translation_path(
            self.artifacts,
            image_id,
            self.target,
        )
        _write_json_atomic(translation_path, phase2)
        translation_input_fingerprint = str(
            prepared.get("translation_input_fingerprint", "")
        )
        ocr_path = Path(prepared["ocr_path"])
        translation_settings = _translation_settings_fingerprint(
            self.config,
            self.target,
        )
        provider_identity = _provider_identity(self.config)
        model_identity = _model_identity(self.config)
        translation_metadata = {
            "target_language": self.target,
            "provider_used": outcome.provider_id,
            "model_used": str(
                getattr(page_result, "model_id", "") or ""
            ),
            "legacy_page_cache_key": str(
                prepared.get("legacy_page_cache_key", "")
            ),
        }
        _record_stage_completion(job_manifest,
            image_id,
            "translating",
            input_fingerprint=translation_input_fingerprint,
            input_artifacts={"ocr_result": ocr_path},
            artifacts={"translation_result": translation_path},
            source_path=source,
            application_version=__version__,
            settings_fingerprint=translation_settings,
            provider_identity=provider_identity,
            model_identity=model_identity,
            metadata=translation_metadata,
        )
        render_input_fingerprint = _render_stage_fingerprint(
            source,
            translation_path,
            target=self.target,
            config=self.config,
        )
        job_manifest.mark(image_id, "rendering", stage="translating")
        self.stage.emit(image_id, "reconstructing", position, len(self.items), f"Rebuilding {source.name}")
        started = time.perf_counter()
        render_dir = target_render_dir(self.artifacts, image_id, self.target)
        render_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".fast-render-{image_id}-",
            dir=render_dir.parent,
        ) as staging_raw:
            staging_dir = Path(staging_raw)
            render_request = RenderRequest(
                request_id=f"batch:{image_id}",
                project_id=str(self.config.get("project_id") or ""),
                image_id=image_id,
                image_index=int(item.get("image_index", position - 1)),
                result_path=translation_path,
                render_dir=staging_dir,
                source_path=source,
                reason="batch",
            )
            render_report = self._render_translation_payload(render_request)
            staged_files = sorted(
                (path for path in staging_dir.rglob("*") if path.is_file()),
                key=lambda path: (
                    path.name == rendered_filename(source, self.target),
                    path.as_posix(),
                ),
            )
            for staged in staged_files:
                destination = render_dir / staged.relative_to(staging_dir)
                destination.parent.mkdir(parents=True, exist_ok=True)
                staged.replace(destination)
            serialized_report = json.dumps(render_report, ensure_ascii=False)
            render_report = json.loads(
                serialized_report.replace(str(staging_dir), str(render_dir))
            )
        timing["stages"]["render_seconds"] = round(time.perf_counter() - started, 3)
        render_json = render_dir / f"{source.stem}_render.json"
        render_details = json.loads(render_json.read_text(encoding="utf-8")) if render_json.is_file() else {}
        rendered_by_group = {
            int(value.get("group", 0)): value for value in render_details.get("rendered_groups", [])
        }
        page_size = tuple(int(value) for value in prepared["page_size"])
        typeset_reviews = [
            review_rendered_group(group, rendered_by_group.get(int(group.get("index", 0) or 0), {}), page_size)
            for group in translated_groups if group.get("status") in {"translated", "review"}
        ]
        render_review = summarize_render_review(typeset_reviews)
        ai_review = review_translation_groups(translated_groups, render_review)
        fast_page = prepared["fast_page"]
        page = self._build_page_dialogue(
            str(fast_page["source_language"]),
            str(fast_page["target_language"]),
            list(fast_page["dialogue"]),
            str(fast_page.get("page_context", "")),
        )
        self._learn_translation_groups(page, page_result, translated_groups)
        phase2["translation_groups"] = translated_groups
        phase2["render_review"] = render_review
        phase2["ai_review"] = ai_review
        _write_json_atomic(translation_path, phase2)
        _record_stage_completion(job_manifest,
            image_id,
            "translating",
            input_fingerprint=translation_input_fingerprint,
            input_artifacts={"ocr_result": ocr_path},
            artifacts={"translation_result": translation_path},
            source_path=source,
            application_version=__version__,
            settings_fingerprint=translation_settings,
            provider_identity=provider_identity,
            model_identity=model_identity,
            metadata=translation_metadata,
        )
        timing["total_seconds"] = round(time.perf_counter() - float(prepared["image_started"]), 3)
        timing["rss_after_page_mb"] = current_rss_mb()
        timing["paths"] = {
            "ocr_result": prepared["ocr_path"],
            "translation_result": str(translation_path),
            "render_dir": str(render_dir),
        }
        intelligent = IntelligentPageResult(
            pipeline_version=__version__,
            image_id=image_id,
            source=str(source.resolve()),
            target_language=self.target,
            preprocessing=prepared["preprocessed"],
            ocr_attempts=ocr_result.metadata.get("manager", {}),
            layout_graph=prepared["layout_graph"],
            bubble_segmentation=bubble_segmentations,
            translation_units=prepared["layout_graph"]["translation_units"],
            render_review={"typesetting": render_review, "ai_review": ai_review, "render_report": render_report},
            debug_artifacts=prepared["debug_artifacts"],
            timing=timing,
        )
        intelligent_path = (
            self.target_artifacts / f"{image_id}_intelligent_page.json"
        )
        _write_json_atomic(intelligent_path, intelligent.to_dict())
        job_manifest.mark(image_id, "review", stage="rendering")
        self._write_image_timing(image_id, timing)
        final_path = render_dir / rendered_filename(source, self.target)
        preview_path = render_dir / f"{source.stem}_preview.png"
        render_json = render_dir / f"{source.stem}_render.json"
        _record_stage_completion(job_manifest,
            image_id,
            "rendering",
            input_fingerprint=render_input_fingerprint,
            input_artifacts={
                "translation_result": translation_path,
            },
            artifacts=_existing_artifacts(
                rendered_image=final_path,
                preview_image=preview_path,
                render_report=render_json,
            ),
            source_path=source,
            application_version=__version__,
            settings_fingerprint=_render_settings_fingerprint(
                self.config,
                self.target,
            ),
            metadata={"target_language": self.target},
        )
        render_outputs = _existing_artifacts(
            rendered_image=final_path,
            preview_image=preview_path,
            render_report=render_json,
        )
        _record_stage_completion(job_manifest,
            image_id,
            "review",
            input_fingerprint=_stable_json_hash({
                "kind": "review-stage-v1",
                "render_input_fingerprint": render_input_fingerprint,
                "render_review": render_review,
                "ai_review": ai_review,
            }),
            input_artifacts=render_outputs,
            artifacts={
                "reviewed_translation": translation_path,
                "intelligent_page": intelligent_path,
            },
            source_path=source,
            application_version=__version__,
            settings_fingerprint=_render_settings_fingerprint(
                self.config,
                self.target,
            ),
            metadata={"target_language": self.target},
        )
        review = any(group["status"] == "review" for group in translated_groups) or ai_review["issue_count"] > 0
        job_manifest.mark(image_id, "done", stage="review")
        self.image_finished.emit(image_id, {
            "status": "review" if review else "ready",
            "source_language": prepared["source_language"],
            "ocr_result": prepared["ocr_path"],
            "translation_result": str(translation_path),
            "rendered_image": str(final_path),
            "preview_image": str(preview_path),
            "error": "",
        })
        return timing

    def _run_fast(self) -> bool:
        cancelled = False
        batch_started = time.perf_counter()
        batch_timings: list[dict[str, Any]] = []
        ocr_service: OCRService | None = None
        translation_executor: ThreadPoolExecutor | None = None
        try:
            self.artifacts.mkdir(parents=True, exist_ok=True)
            requested_source = self.config.get("source_language", "auto")
            preferred = _initial_ocr_language(
                requested_source, "Fast", self.config.get("auto_primary_language") or None,
            )
            ocr_service = OCRService(
                _ocr_engine_languages(requested_source, "Fast", preferred),
                use_subprocess=bool(self.config.get("ocr_subprocess_enabled", False)),
                recycle_pages=int(self.config.get("ocr_worker_recycle_pages", 25)),
                memory_limit_mb=int(self.config.get("ocr_worker_memory_limit_mb", 2048)),
                retry_stats_path=PATHS.cache / "ocr_retry_stats.json",
            )
            self.target_artifacts.mkdir(parents=True, exist_ok=True)
            job_manifest = JobManifest.load(
                target_manifest_path(self.artifacts, self.target)
            )
            session = TRANSLATION_RUNTIME.fast_session(
                self.config,
                cancel_event=self.cancel,
            )
            primary_provider = str(
                getattr(session, "primary", "")
                or self.config.get("translation_engine", "qwen")
            ).strip().lower()
            stream_page_translations = primary_provider == "openai_compatible"
            prepared: list[dict[str, Any]] = []

            def build_fast_job(
                descriptor: dict[str, Any],
                context: str,
            ) -> ParallelPageJob | None:
                payload = json.loads(Path(descriptor["path"]).read_text(encoding="utf-8"))
                page = self._build_page_dialogue(
                    str(payload["source_language"]), self.target,
                    list(payload["dialogue"]), context,
                )
                cache_key = _page_translation_cache_key(page, self.config, self.target)
                translation_input_fingerprint = (
                    _translation_stage_fingerprint(
                        page,
                        self.config,
                        self.target,
                    )
                )
                cache_path = PATHS.page_translation_cache / (
                    f"{_cache_fragment(Path(payload['source']).stem)}_{cache_key[:16]}.json"
                )
                payload["fast_page"] = {
                    "source_language": page.source_language,
                    "target_language": page.target_language,
                    "dialogue": page.dialogue,
                    "page_context": page.page_context,
                }
                payload["translation_input_fingerprint"] = (
                    translation_input_fingerprint
                )
                payload["legacy_page_cache_key"] = cache_key
                _write_json_atomic(Path(descriptor["path"]), payload)
                source_item = next(
                    item
                    for item in self.items
                    if str(item["id"]) == str(descriptor["image_id"])
                )
                if self._resume_verified_translation(
                    source_item,
                    int(descriptor["position"]),
                    len(self.items),
                    job_manifest,
                    translation_input_fingerprint=(
                        translation_input_fingerprint
                    ),
                ):
                    return None
                job_manifest.mark(descriptor["image_id"], "queued", stage="OCR")
                self.stage.emit(
                    descriptor["image_id"], "queued", descriptor["position"], len(self.items),
                    (
                        f"Queued {Path(payload['source']).name} for page translation"
                        if stream_page_translations
                        else f"Queued {Path(payload['source']).name} for smart translation"
                    ),
                )
                return ParallelPageJob(
                    page_index=int(descriptor["position"]) - 1,
                    image_id=descriptor["image_id"],
                    request_id=f"batch:{descriptor['image_id']}",
                    prepared_path=Path(descriptor["path"]),
                    cache_path=cache_path,
                    page=page,
                )

            def commit(outcome: PageTranslationOutcome) -> PageTranslationOutcome:
                try:
                    timing = self._commit_fast_outcome(outcome, job_manifest)
                except Exception as error:
                    message = f"{type(error).__name__}: {error}"
                    if outcome.image_id in job_manifest.pages:
                        job_manifest.mark(outcome.image_id, "failed", error=message)
                    self.image_failed.emit(outcome.image_id, message)
                    timing = {
                        "image_id": outcome.image_id,
                        "position": outcome.page_index + 1,
                        "error": message,
                        "traceback": traceback.format_exc(),
                    }
                    self._write_image_timing(outcome.image_id, timing)
                    outcome = PageTranslationOutcome(
                        page_index=outcome.page_index,
                        image_id=outcome.image_id,
                        request_id=outcome.request_id,
                        provider_id=outcome.provider_id,
                        attempts=outcome.attempts,
                        elapsed_seconds=outcome.elapsed_seconds,
                        error=message,
                    )
                if timing is not None:
                    batch_timings.append(timing)
                return outcome

            active_stream: dict[Future[PageTranslationOutcome], ParallelPageJob] = {}
            translation_executor = (
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="HydraOpenAICompatibleFast")
                if stream_page_translations
                else None
            )

            def drain_stream(*, wait_for_all: bool = False) -> None:
                if translation_executor is None:
                    return
                while active_stream and not self.cancel.is_set():
                    done, _pending = wait(
                        tuple(active_stream),
                        timeout=None if wait_for_all else 0,
                        return_when=FIRST_COMPLETED,
                    )
                    if not done:
                        return
                    for future in sorted(
                        done,
                        key=lambda item: active_stream[item].page_index,
                    ):
                        job = active_stream.pop(future)
                        try:
                            outcome = future.result()
                        except Exception as error:
                            outcome = PageTranslationOutcome(
                                page_index=job.page_index,
                                image_id=job.image_id,
                                request_id=job.request_id,
                                attempts=1,
                                error=f"{type(error).__name__}: {error}",
                            )
                        if outcome.error:
                            LOGGER.warning(
                                "OpenAI-compatible page translation failed image_id=%s page_index=%d error=%s",
                                outcome.image_id,
                                outcome.page_index,
                                outcome.error,
                            )
                        commit(outcome)
                    if not wait_for_all:
                        return

            for position, item in enumerate(self.items, 1):
                if self.cancel.is_set():
                    cancelled = True
                    break
                if self._resume_verified_render(
                    item,
                    position,
                    len(self.items),
                    job_manifest,
                ):
                    continue
                try:
                    prepared.append(self._prepare_fast_page(
                        item, position, len(self.items), ocr_service,
                        requested_source, preferred, job_manifest,
                    ))
                    if stream_page_translations:
                        context = self._fast_page_context(prepared, len(prepared) - 1)
                        job = build_fast_job(prepared[-1], context)
                        if job is not None and translation_executor is not None:
                            active_stream[
                                translation_executor.submit(
                                    self._translate_fast_job,
                                    session,
                                    job,
                                )
                            ] = job
                        drain_stream()
                except RequestCancelled:
                    cancelled = True
                    break
                except Exception as error:
                    image_id = str(item["id"])
                    job_manifest.ensure_page(image_id, str(item["source_path"]))
                    job_manifest.mark(image_id, "failed", error=f"{type(error).__name__}: {error}")
                    self.image_failed.emit(image_id, f"{type(error).__name__}: {error}")
                finally:
                    gc.collect(0)
            if stream_page_translations:
                drain_stream(wait_for_all=True)
                return cancelled or self.cancel.is_set()
            if cancelled or not prepared:
                return cancelled
            smart_jobs: list[SmartPageJob] = []
            for index, descriptor in enumerate(prepared):
                context = self._fast_page_context(prepared, index)
                page_job = build_fast_job(descriptor, context)
                if page_job is None:
                    continue
                smart_jobs.append(SmartPageJob(
                    page_index=page_job.page_index,
                    image_id=page_job.image_id,
                    request_id=page_job.request_id,
                    prepared_path=page_job.prepared_path,
                    cache_path=page_job.cache_path,
                    bubble_cache_dir=(
                        PATHS.page_translation_cache
                        / "bubble_scheduler"
                        / _cache_fragment(str(descriptor["image_id"]))
                    ),
                    page=page_job.page,
                ))
            # Prepared OCR text was needed only to build immutable context. The
            # scheduler now retains disk descriptors, not every page payload.
            prepared.clear()

            def snapshot(value: SchedulerSnapshot) -> None:
                self.scheduler_snapshot.emit(value)

            scheduler = SmartTranslationScheduler(
                primary_provider=str(getattr(session, "primary", "") or self.config.get("translation_engine", "qwen")),
                fallback_provider=str(getattr(session, "fallback", "") or self.config.get("translation_fallback_engine", "")),
                glossary=dict(self.config.get("glossary", {}) or {}),
                worker_override=int(self.config.get("fast_worker_override", 0) or 0),
                translation_memory_enabled=bool(
                    self.config.get("translation_memory_enabled", True)
                ),
                prefer_verified_tm=bool(
                    self.config.get("translation_memory_prefer_verified", True)
                ),
                cancel_event=self.cancel,
                gpu_state=session.gpu_state,
                snapshot_callback=snapshot,
            )
            try:
                scheduler.run(
                    smart_jobs,
                    ProviderDispatcher(session),
                    commit,
                )
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                for job in smart_jobs:
                    if job.image_id in job_manifest.pages:
                        job_manifest.mark(job.image_id, "failed", error=message)
                    self.image_failed.emit(job.image_id, message)
                raise
            cancelled = self.cancel.is_set()
        except Exception as error:
            self.image_failed.emit("", f"Fast pipeline initialization failed: {type(error).__name__}: {error}")
        finally:
            if translation_executor is not None:
                translation_executor.shutdown(wait=False, cancel_futures=True)
            if ocr_service is not None:
                ocr_service.close()
            self._write_batch_timing(batch_timings, time.perf_counter() - batch_started)
        return cancelled

    @Slot()
    def run(self) -> None:
        if str(self.config.get("quality", "Balanced")) == "Fast":
            self.finished.emit(self._run_fast())
            return
        cancelled = False
        try:
            self.artifacts.mkdir(parents=True, exist_ok=True)
            requested_source = self.config.get("source_language", "auto")
            quality = self.config.get("quality", "Balanced")
            preferred = _initial_ocr_language(
                requested_source, quality, self.config.get("auto_primary_language") or None,
            )
            ocr_service = OCRService(
                _ocr_engine_languages(requested_source, quality, preferred),
                use_subprocess=bool(self.config.get("ocr_subprocess_enabled", False)),
                recycle_pages=int(self.config.get("ocr_worker_recycle_pages", 25)),
                memory_limit_mb=int(self.config.get("ocr_worker_memory_limit_mb", 2048)),
                retry_stats_path=PATHS.cache / "ocr_retry_stats.json",
            )
            chapter_ocr_language = preferred
            total = len(self.items)
            batch_started = time.perf_counter()
            batch_timings: list[dict[str, Any]] = []
            translation_pool = BoundedStageExecutor(
                max_workers=int(self.config.get("translation_concurrency", 2)),
                queue_capacity=max(2, int(self.config.get("translation_concurrency", 2)) * 2),
                name="HydraTranslation",
            )
            context_engine = ContextEngine(glossary=dict(self.config.get("glossary", {})))
            self.target_artifacts.mkdir(parents=True, exist_ok=True)
            job_manifest = JobManifest.load(
                target_manifest_path(self.artifacts, self.target)
            )
            
            for position, item in enumerate(self.items, 1):
                image_id, source = item["id"], Path(item["source_path"])
                job_manifest.ensure_page(image_id, str(source))
                if self._resume_verified_render(
                    item,
                    position,
                    total,
                    job_manifest,
                ):
                    continue
                image_started = time.perf_counter()
                timing: dict[str, Any] = {
                    "image_id": image_id,
                    "source": str(source),
                    "position": position,
                    "total": total,
                    "source_language_request": requested_source,
                    "quality": quality,
                    "stages": {},
                    "counts": {},
                    "retry": {
                        "attempt_count": 0,
                        "accepted_count": 0,
                        "rejected_count": 0,
                        "by_reason": {},
                    },
                    "cache": {
                        "ocr_hit": False,
                        "translation_hit": False,
                    },
                }
                if self.cancel.is_set():
                    cancelled = True
                    break
                source_image = None
                try:
                    job_manifest.mark(image_id, "preprocessing")
                    self.stage.emit(image_id, "preprocessing", position, total, f"Preparing {source.name}")
                    stage_started = time.perf_counter()
                    ocr_path = self.artifacts / f"{image_id}_ocr.json"
                    preprocessed = prepare_ocr_image(source, self.artifacts / image_id / "preprocess")
                    source_for_ocr = Path(preprocessed.ocr_path)
                    timing["stages"]["preprocess_seconds"] = round(time.perf_counter() - stage_started, 3)
                    timing["preprocessing"] = preprocessed.to_dict()
                    preprocessing_settings = _preprocessing_settings_fingerprint(
                        self.config,
                        quality,
                    )
                    _record_stage_completion(job_manifest,
                        image_id,
                        "preprocessing",
                        input_fingerprint=_preprocessing_stage_fingerprint(
                            source,
                            preprocessing_settings,
                        ),
                        input_artifacts={"source_image": source},
                        artifacts={
                            "preprocessed_image": source_for_ocr,
                        },
                        source_path=source,
                        application_version=__version__,
                        settings_fingerprint=preprocessing_settings,
                        metadata={"quality": quality},
                    )
                    if self.cancel.is_set():
                        cancelled = True
                        break

                    stage_started = time.perf_counter()
                    ocr_cache_dir = PATHS.ocr_cache
                    ocr_cache_dir.mkdir(parents=True, exist_ok=True)
                    cache_preferred = None if quality == "Maximum" else chapter_ocr_language
                    ocr_cache_key = _stable_json_hash({
                        "kind": "ocr-v3-preprocessed",
                        "base": _ocr_cache_key(source, requested_source, quality, cache_preferred),
                        "preprocessing": preprocessed.quality.to_dict(),
                    })
                    ocr_cache_path = ocr_cache_dir / f"{_cache_fragment(source.stem)}_{ocr_cache_key[:16]}.json"
                    ocr_settings = _ocr_settings_fingerprint(
                        requested_source,
                        quality,
                        cache_preferred,
                        preprocessed.quality.to_dict(),
                    )
                    ocr_plan = _state_manager_for_stages(
                        job_manifest,
                        StageContract("OCR"),
                    ).plan_page(
                        image_id,
                        source,
                        {
                            "OCR": StageValidationRequest(
                                input_fingerprint=ocr_cache_key,
                                artifacts={"ocr_result": ocr_path},
                                input_artifacts={
                                    "preprocessed_image": source_for_ocr,
                                },
                                source_path=source,
                                application_version=__version__,
                                settings_fingerprint=ocr_settings,
                            ),
                        },
                    )
                    force_retranslate = bool(self.config.get("force_retranslate", False))
                    verified_checkpoint = (
                        ocr_plan.action_for("OCR") is StageAction.SKIP
                        and not force_retranslate
                    )
                    if not verified_checkpoint:
                        job_manifest.mark(image_id, "OCR", stage="preprocessing")
                        self.stage.emit(
                            image_id,
                            "ocr",
                            position,
                            total,
                            f"Reading text in {source.name}",
                        )
                    service_result = self._run_ocr_page(
                        ocr_service,
                        source_for_ocr,
                        preferred_language=cache_preferred,
                        quality=quality,
                        auto_language_fallback=quality == "Maximum" and requested_source == "auto" and chapter_ocr_language is not None,
                        cache_path=None if force_retranslate else ocr_cache_path,
                        checkpoint_path=ocr_path if verified_checkpoint else None,
                    )
                    ocr_result = service_result.ocr_result
                    source_regions = service_result.final_regions
                    timing["cache"]["ocr_hit"] = service_result.cache_hit
                    timing["cache"]["ocr_checkpoint_verified"] = verified_checkpoint
                    timing["ocr_service"] = service_result.telemetry
                    manager_summary = dict(ocr_result.metadata.get("manager", {}).get("retry_summary", {}))
                    timing["retry"] = {
                        **timing["retry"],
                        "attempt_count": int(manager_summary.get("attempt_count", 0) or 0),
                        "accepted_count": int(manager_summary.get("accepted_count", 0) or 0),
                        "rejected_count": int(manager_summary.get("rejected_count", 0) or 0),
                        "by_reason": dict(manager_summary.get("by_reason", {})),
                    }
                    timing["stages"]["ocr_seconds"] = round(time.perf_counter() - stage_started, 3)
                    timing["rss_after_ocr_mb"] = current_rss_mb()
                    if requested_source == "auto":
                        chapter_ocr_language = ocr_result.model_language
                    source_language = resolve_source_language(requested_source, ocr_result.language)
                    _write_json_atomic(ocr_path, ocr_result.to_dict())
                    _record_stage_completion(job_manifest,
                        image_id,
                        "OCR",
                        input_fingerprint=ocr_cache_key,
                        artifacts={"ocr_result": ocr_path},
                        input_artifacts={
                            "preprocessed_image": source_for_ocr,
                        },
                        source_path=source,
                        application_version=__version__,
                        settings_fingerprint=ocr_settings,
                        metadata={
                            "quality": quality,
                            "source_language_request": requested_source,
                            "preferred_language": cache_preferred or "",
                        },
                    )
                    if self.cancel.is_set():
                        cancelled = True
                        break

                    job_manifest.mark(image_id, "translating", stage="OCR")
                    self.stage.emit(image_id, "translating", position, total, f"Translating {source.name}")
                    stage_started = time.perf_counter()
                    with Image.open(source) as opened:
                        source_image = opened.convert("RGB")
                        page_size = source_image.size
                    layout_graph = build_layout_graph(source_regions, page_size=page_size)
                    groups = group_regions(source_regions)
                    group_payloads = []
                    group_source_polygons = []
                    effective_groups = []
                    
                    for group in groups:
                        effective_group, text_member_indices, decorative_symbols = _text_group_without_preserved_marks(
                            group,
                            source_regions,
                        )
                        effective_groups.append(effective_group)
                        text = effective_group.text
                        placement_bbox = effective_group.bbox
                        confidence = min(source_regions[index - 1]["confidence"] for index in effective_group.member_indices)
                        polygons = [source_regions[index - 1]["polygon"] for index in text_member_indices]
                        cleanup_polygons = [
                            *polygons,
                            *[
                                symbol.get("polygon")
                                for symbol in decorative_symbols
                                if isinstance(symbol, dict) and symbol.get("polygon")
                            ],
                        ]
                        source_text_colors = [_source_text_color(source_image, polygon) for polygon in polygons]

                        bubble_type = _classify_bubble(effective_group)
                        classification = classify_text_group(effective_group, source_regions, ocr_result.model_language)
                        if bubble_type not in {"credit", "sfx"}:
                            bubble_type = classification.kind
                        candidate_payload = {
                            "type": bubble_type,
                            "text": text,
                            "original_text": text,
                            "polygon": [[placement_bbox[0], placement_bbox[1]], [placement_bbox[2], placement_bbox[1]], [placement_bbox[2], placement_bbox[3]], [placement_bbox[0], placement_bbox[3]]],
                            "source_direction": group.direction,
                            "source_polygons": polygons,
                            "cleanup_polygons": cleanup_polygons,
                            "source_text_colors": source_text_colors,
                            "decorative_symbols": decorative_symbols,
                            "preserved_marks": [],
                        }
                        if bubble_type not in {"credit", "sfx", "sign"} and detect_title_objects([candidate_payload], page_size):
                            bubble_type = "title"

                        group_payloads.append({
                            "type": bubble_type,
                            "text": text,
                            "confidence": confidence,
                            "classification_reasons": classification.reasons,
                            "ocr_review_reasons": list(dict.fromkeys(
                                str(reason)
                                for member_index in group.member_indices
                                for reason in source_regions[member_index - 1].get("ocr_review_reasons", [])
                                if str(reason)
                            )),
                            "polygon": [[placement_bbox[0], placement_bbox[1]], [placement_bbox[2], placement_bbox[1]], [placement_bbox[2], placement_bbox[3]], [placement_bbox[0], placement_bbox[3]]],
                            "source_text_hash": source_text_hash(text),
                            "source_region_hash": source_region_hash(
                                source_image,
                                polygons,
                            ),
                            "cleanup_polygons": cleanup_polygons,
                            "decorative_symbols": decorative_symbols,
                            "preserved_marks": [],
                        })
                        group_source_polygons.append(polygons)

                    debug_artifacts = (
                        self._write_ocr_debug_artifacts(source, image_id, source_regions, effective_groups, layout_graph.to_dict())
                        if bool(self.config.get("debug_artifacts_enabled", False))
                        else {}
                    )
                    timing["counts"]["source_regions"] = len(source_regions)
                    timing["counts"]["groups"] = len(groups)
                    timing["counts"]["layout_edges"] = len(layout_graph.edges)

                    # Filter dialogue array using the payload dictionary type
                    dialogue = []
                    for idx, payload in enumerate(group_payloads):
                        region_type = normalize_region_type(payload.get("type"))
                        if not _auto_translate_region_type(region_type, self.config):
                            continue
                        dialogue.append({
                            "id": f"r{idx + 1}",
                            "text": payload["text"],
                            "confidence": payload.get("confidence", 1.0),
                            "source_text": str(effective_groups[idx].text).strip() if idx < len(effective_groups) else "",
                            "reading_order": len(dialogue) + 1,
                            "source_direction": effective_groups[idx].direction if idx < len(effective_groups) else "",
                            "bbox": payload.get("polygon", []),
                            "region_type": region_type,
                            "source_text_hash": payload.get("source_text_hash", ""),
                            "source_region_hash": payload.get("source_region_hash"),
                            "decorative_symbols": payload.get("decorative_symbols", []),
                            "preserved_marks": payload.get("preserved_marks", []),
                        })

                    precomputed_segmentations = None
                    page_context = (
                        context_engine.page_context(
                            dialogue,
                            layout_graph.to_dict(),
                            position,
                        )
                        if dialogue
                        else ""
                    )
                    page = self._build_page_dialogue(
                        source_language,
                        self.target,
                        dialogue,
                        page_context,
                    )
                    page_cache_key = _page_translation_cache_key(
                        page,
                        self.config,
                        self.target,
                    )
                    translation_input_fingerprint = (
                        _translation_stage_fingerprint(
                            page,
                            self.config,
                            self.target,
                        )
                    )
                    if self._resume_verified_translation(
                        item,
                        position,
                        total,
                        job_manifest,
                        translation_input_fingerprint=(
                            translation_input_fingerprint
                        ),
                    ):
                        continue
                    if not dialogue:
                        page_result = PageTranslation(source_language, self.target, [])
                    else:
                        translation_cache_dir = PATHS.page_translation_cache
                        translation_cache_dir.mkdir(parents=True, exist_ok=True)
                        page_cache_path = translation_cache_dir / f"{_cache_fragment(source.stem)}_{page_cache_key[:16]}.json"
                        if (
                            not bool(self.config.get("force_retranslate", False))
                            and page_cache_path.exists()
                        ):
                            page_payload = json.loads(page_cache_path.read_text(encoding="utf-8"))
                            cached_result = PageTranslation(
                                source_language=str(page_payload.get("source_language", page.source_language)),
                                target_language=str(page_payload.get("target_language", page.target_language)),
                                translations=list(page_payload.get("translations", [])),
                            )
                            try:
                                page_result = TRANSLATION_RUNTIME.translate_cached_page(
                                    page,
                                    cached_result,
                                    self.config,
                                )
                                timing["cache"]["translation_hit"] = True
                            except (OSError, RuntimeError, TypeError, ValueError):
                                page_result = self._translate_page_dialogue(page)
                                _write_json_atomic(page_cache_path, asdict(page_result))
                        else:
                            translation_future = translation_pool.submit(
                                self._translate_page_dialogue, page,
                            )
                            if bool(self.config.get("streaming_enabled", True)):
                                precomputed_segmentations = self._segment_groups(
                                    source, image_id, effective_groups, group_payloads,
                                )
                            page_result = translation_future.result()
                            _write_json_atomic(page_cache_path, asdict(page_result))
                    timing["stages"]["translate_seconds"] = round(time.perf_counter() - stage_started, 3)

                    translated_map = {str(item.get("id")): normalize_global_text(str(item.get("text", ""))) for item in page_result.translations}
                    translated_details = {
                        str(item.get("id")): dict(item)
                        for item in page_result.translations
                    }
                    
                    # Deduplicate Qwen hallucinations: if the translated text repeats the same
                    # phrase twice, keep only one occurrence. Handles cases like:
                    # "I want it all! I want it all!" -> "I want it all!"
                    # "Everything's fine! Everything's fine!" -> "Everything's fine!"
                    for key in translated_map:
                        text = translated_map[key]
                        stripped = text.strip().rstrip(".!?")
                        half = len(stripped) // 2
                        if half >= 4:
                            first = stripped[:half].strip().rstrip(".,!?;: ")
                            second = stripped[half:].strip().rstrip(".,!?;: ")
                            if first == second or second.startswith(first) or first.startswith(second):
                                translated_map[key] = first.rstrip(".,!?;: ") + "!"
                    
                    translated_groups = []
                    bubble_segmentations = precomputed_segmentations or self._segment_groups(
                        source, image_id, effective_groups, group_payloads,
                    )
                    
                    for idx, (group, polygons, payload, segmentation_payload) in enumerate(zip(
                        effective_groups, group_source_polygons, group_payloads, bubble_segmentations,
                    )):
                        region_index = idx + 1
                        bubble_id = f"r{region_index}"
                        
                        region_type = normalize_region_type(payload.get("type"))
                        translate_enabled = _auto_translate_region_type(region_type, self.config)
                        if not translate_enabled:
                            translated_text = ""
                            status = "preserved" 
                            confidence_score = 1.0
                            review_reasons = list(payload.get("classification_reasons", [])) or [f"{region_type}_translation_disabled"]
                        else:
                            translated_text = normalize_global_text(translated_map.get(bubble_id, ""))
                            confidence_score = 0.0 if not translated_text or translated_text == group.text else 1.0
                            review_reasons = list(payload.get("classification_reasons", []))
                            review_reasons.extend(
                                f"ocr:{reason}" for reason in payload.get("ocr_review_reasons", [])
                            )
                            if confidence_score <= 0.5:
                                review_reasons.append("low_confidence_or_empty")
                            status = "review" if review_reasons else "translated"
                            
                        translated_groups.append({
                            "index": region_index,
                            "original_text": str(group.text).strip(),
                            "literal_text": str(group.text).strip(),
                            "translated_text": translated_text,
                            "type": region_type,
                            "bubble_type": region_type,
                            "ocr_confidence": float(min(source_regions[i - 1]["confidence"] for i in group.member_indices)),
                            "polygon": payload.get("polygon", [[group.bbox[0], group.bbox[1]], [group.bbox[2], group.bbox[1]], [group.bbox[2], group.bbox[3]], [group.bbox[0], group.bbox[3]]]),
                            "status": status,
                            "review_reasons": list(dict.fromkeys(review_reasons)),
                            "alternatives": [],
                            "provider": str(
                                translated_details.get(bubble_id, {}).get(
                                    "provider_id"
                                )
                                or (
                                    "page-cache"
                                    if timing["cache"]["translation_hit"]
                                    else TRANSLATION_RUNTIME.last_engine_id
                                )
                            ),
                            "translation_source": str(
                                translated_details.get(bubble_id, {}).get(
                                    "translation_source",
                                    "provider",
                                )
                            ),
                            "tm_entry_id": translated_details.get(
                                bubble_id,
                                {},
                            ).get("tm_entry_id"),
                            "source_text_hash": payload.get("source_text_hash", ""),
                            "source_region_hash": payload.get(
                                "source_region_hash"
                            ),
                            "model": "",
                            "localization_style": self.config.get("localization_style", "Manga"),
                            "translation_quality": "good" if status == "translated" else "review",
                            "localization_note": "",
                            "member_region_indices": group.member_indices,
                            "source_direction": group.direction,
                            "direction": "horizontal-ltr",
                            "source_polygons": polygons,
                            "cleanup_polygons": list(payload.get("cleanup_polygons", [])),
                            "decorative_symbols": list(payload.get("decorative_symbols", [])),
                            "preserved_marks": list(payload.get("preserved_marks", [])),
                            "source_member_texts": [str(source_regions[i - 1].get("text", "")) for i in group.member_indices],
                            "source_text_colors": list(payload.get("source_text_colors", [])),
                            "bubble_segmentation": segmentation_payload,
                            "bubble_mask": str(segmentation_payload.get("mask_path", "")),
                            "safe_area": list(segmentation_payload.get("safe_area", [])),
                        })

                    phase2 = self._build_translation_payload(
                        source=source,
                        source_language=source_language,
                        ocr_result=ocr_result,
                        source_regions=source_regions,
                        translated_groups=translated_groups,
                        preprocessed=preprocessed,
                        layout_graph=layout_graph.to_dict(),
                        bubble_segmentations=bubble_segmentations,
                    )
                    translation_path = target_translation_path(
                        self.artifacts,
                        image_id,
                        self.target,
                    )
                    _write_json_atomic(translation_path, phase2)
                    _record_stage_completion(job_manifest,
                        image_id,
                        "translating",
                        input_fingerprint=translation_input_fingerprint,
                        input_artifacts={"ocr_result": ocr_path},
                        artifacts={"translation_result": translation_path},
                        source_path=source,
                        application_version=__version__,
                        settings_fingerprint=(
                            _translation_settings_fingerprint(
                                self.config,
                                self.target,
                            )
                        ),
                        provider_identity=_provider_identity(self.config),
                        model_identity=_model_identity(self.config),
                        metadata={
                            "target_language": self.target,
                            "provider_used": str(
                                TRANSLATION_RUNTIME.last_engine_id
                                or (
                                    page_result.translations[0].get(
                                        "provider_id",
                                        "",
                                    )
                                    if page_result.translations
                                    else ""
                                )
                            ),
                            "legacy_page_cache_key": page_cache_key,
                        },
                    )
                    render_input_fingerprint = _render_stage_fingerprint(
                        source,
                        translation_path,
                        target=self.target,
                        config=self.config,
                    )
                    if self.cancel.is_set():
                        cancelled = True
                        break

                    job_manifest.mark(image_id, "rendering", stage="translating")
                    self.stage.emit(image_id, "reconstructing", position, total, f"Rebuilding {source.name}")
                    stage_started = time.perf_counter()
                    render_dir = target_render_dir(
                        self.artifacts,
                        image_id,
                        self.target,
                    )
                    render_request = RenderRequest(
                        request_id=f"batch:{image_id}",
                        project_id=str(self.config.get("project_id") or ""),
                        image_id=image_id,
                        image_index=int(item.get("image_index", position - 1)),
                        result_path=translation_path,
                        render_dir=render_dir,
                        source_path=source,
                        reason="batch",
                    )
                    render_report = self._render_translation_payload(render_request)
                    timing["stages"]["render_seconds"] = round(time.perf_counter() - stage_started, 3)
                    render_json = render_dir / f"{source.stem}_render.json"
                    render_details = {}
                    if render_json.is_file():
                        render_details = json.loads(render_json.read_text(encoding="utf-8"))
                    rendered_by_group = {
                        int(item.get("group", 0)): item
                        for item in render_details.get("rendered_groups", [])
                    }
                    typeset_reviews = [
                        review_rendered_group(group, rendered_by_group.get(int(group.get("index", 0) or 0), {}), page_size)
                        for group in translated_groups
                        if group.get("status") in {"translated", "review"}
                    ]
                    render_review = summarize_render_review(typeset_reviews)
                    ai_review = review_translation_groups(translated_groups, render_review)
                    if dialogue:
                        self._learn_translation_groups(
                            page,
                            page_result,
                            translated_groups,
                        )
                    phase2["translation_groups"] = translated_groups
                    phase2["render_review"] = render_review
                    phase2["ai_review"] = ai_review
                    _write_json_atomic(translation_path, phase2)
                    _record_stage_completion(job_manifest,
                        image_id,
                        "translating",
                        input_fingerprint=translation_input_fingerprint,
                        input_artifacts={"ocr_result": ocr_path},
                        artifacts={"translation_result": translation_path},
                        source_path=source,
                        application_version=__version__,
                        settings_fingerprint=(
                            _translation_settings_fingerprint(
                                self.config,
                                self.target,
                            )
                        ),
                        provider_identity=_provider_identity(self.config),
                        model_identity=_model_identity(self.config),
                        metadata={
                            "target_language": self.target,
                            "provider_used": str(
                                TRANSLATION_RUNTIME.last_engine_id
                                or (
                                    page_result.translations[0].get(
                                        "provider_id",
                                        "",
                                    )
                                    if page_result.translations
                                    else ""
                                )
                            ),
                            "legacy_page_cache_key": page_cache_key,
                        },
                    )
                    timing["total_seconds"] = round(time.perf_counter() - image_started, 3)
                    timing["rss_after_page_mb"] = current_rss_mb()
                    timing["ocr_worker_restart_count"] = ocr_service.restart_count
                    timing["paths"] = {
                        "ocr_result": str(ocr_path),
                        "translation_result": str(translation_path),
                        "render_dir": str(render_dir),
                        "timing": str(self.target_artifacts / f"{image_id}_timing.json"),
                        "intelligent_page": str(self.target_artifacts / f"{image_id}_intelligent_page.json"),
                    }
                    intelligent = IntelligentPageResult(
                        pipeline_version=__version__,
                        image_id=image_id,
                        source=str(source.resolve()),
                        target_language=self.target,
                        preprocessing=preprocessed.to_dict(),
                        ocr_attempts=ocr_result.metadata.get("manager", {}),
                        layout_graph=layout_graph.to_dict(),
                        bubble_segmentation=bubble_segmentations,
                        translation_units=layout_graph.to_dict()["translation_units"],
                        render_review={"typesetting": render_review, "ai_review": ai_review, "render_report": render_report},
                        debug_artifacts=debug_artifacts,
                        timing=timing,
                    )
                    intelligent_path = (
                        self.target_artifacts
                        / f"{image_id}_intelligent_page.json"
                    )
                    _write_json_atomic(
                        intelligent_path,
                        intelligent.to_dict(),
                    )
                    job_manifest.mark(image_id, "review", stage="rendering")
                    self._write_image_timing(image_id, timing)
                    batch_timings.append(timing)
                    final_path = render_dir / rendered_filename(source, self.target)
                    preview_path = render_dir / f"{source.stem}_preview.png"
                    render_json = render_dir / f"{source.stem}_render.json"
                    _record_stage_completion(job_manifest,
                        image_id,
                        "rendering",
                        input_fingerprint=render_input_fingerprint,
                        input_artifacts={
                            "translation_result": translation_path,
                        },
                        artifacts=_existing_artifacts(
                            rendered_image=final_path,
                            preview_image=preview_path,
                            render_report=render_json,
                        ),
                        source_path=source,
                        application_version=__version__,
                        settings_fingerprint=(
                            _render_settings_fingerprint(
                                self.config,
                                self.target,
                            )
                        ),
                        metadata={"target_language": self.target},
                    )
                    render_outputs = _existing_artifacts(
                        rendered_image=final_path,
                        preview_image=preview_path,
                        render_report=render_json,
                    )
                    _record_stage_completion(job_manifest,
                        image_id,
                        "review",
                        input_fingerprint=_stable_json_hash({
                            "kind": "review-stage-v1",
                            "render_input_fingerprint": (
                                render_input_fingerprint
                            ),
                            "render_review": render_review,
                            "ai_review": ai_review,
                        }),
                        input_artifacts=render_outputs,
                        artifacts={
                            "reviewed_translation": translation_path,
                            "intelligent_page": intelligent_path,
                        },
                        source_path=source,
                        application_version=__version__,
                        settings_fingerprint=(
                            _render_settings_fingerprint(
                                self.config,
                                self.target,
                            )
                        ),
                        metadata={"target_language": self.target},
                    )
                    review = any(group["status"] == "review" for group in translated_groups) or ai_review["issue_count"] > 0
                    job_manifest.mark(image_id, "done", stage="review")
                    context_engine.remember_page(translated_groups)
                    self.image_finished.emit(image_id, {
                        "status": "review" if review else "ready", "source_language": source_language,
                        "ocr_result": str(ocr_path), "translation_result": str(translation_path),
                        "rendered_image": str(final_path), "preview_image": str(preview_path), "error": "",
                    })
                except Exception as error:
                    if image_id in job_manifest.pages:
                        job_manifest.mark(image_id, "failed", error=f"{type(error).__name__}: {error}")
                    timing["error"] = f"{type(error).__name__}: {error}"
                    timing["traceback"] = traceback.format_exc()
                    timing["total_seconds"] = round(time.perf_counter() - image_started, 3)
                    self._write_image_timing(image_id, timing)
                    batch_timings.append(timing)
                    self.image_failed.emit(image_id, f"{type(error).__name__}: {error}")
                finally:
                    # Release large per-page PIL/OpenCV payloads without forcing
                    # an expensive full heap collection after every page.
                    if source_image is not None:
                        source_image.close()
                    source_image = None
                    original_page = None
                    source_regions = None
                    groups = None
                    group_payloads = None
                    translated_groups = None
                    gc.collect(0)
        except Exception as error:
            self.image_failed.emit("", f"Pipeline initialization failed: {type(error).__name__}: {error}")
        finally:
            if "translation_pool" in locals():
                translation_pool.shutdown(wait=True, cancel_futures=True)
            if "ocr_service" in locals():
                ocr_service.close()
            if "batch_timings" in locals():
                self._write_batch_timing(batch_timings, time.perf_counter() - batch_started)
        self.finished.emit(cancelled)

    def _write_image_timing(self, image_id: str, timing: dict[str, Any]) -> None:
        path = self.target_artifacts / f"{image_id}_timing.json"
        _write_json_atomic(path, timing)

    def _segment_groups(
        self, source: Path, image_id: str, groups: list[TextGroup], group_payloads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        mask_dir = self.artifacts / image_id / "bubble_masks"
        with Image.open(source) as opened:
            original_page = opened.convert("RGB")
        results: list[dict[str, Any]] = []
        for index, (group, payload) in enumerate(zip(groups, group_payloads), 1):
            polygon = payload.get("polygon", [
                [group.bbox[0], group.bbox[1]], [group.bbox[2], group.bbox[1]],
                [group.bbox[2], group.bbox[3]], [group.bbox[0], group.bbox[3]],
            ])
            results.append(segment_bubble(
                original_page,
                polygon,
                bubble_type=str(payload.get("type", "speech")),
                padding=int(self.config.get("bubble_padding", 5)),
                mask_path=mask_dir / f"group_{index:03d}.png",
            ).to_dict())
        return results

    def _write_batch_timing(self, image_timings: list[dict[str, Any]], elapsed_seconds: float) -> None:
        summary = {
            "total_seconds": round(elapsed_seconds, 3),
            "image_count": len(image_timings),
            "ocr_cache_hits": sum(1 for item in image_timings if item.get("cache", {}).get("ocr_hit")),
            "translation_cache_hits": sum(1 for item in image_timings if item.get("cache", {}).get("translation_hit")),
            "images": image_timings,
        }
        path = self.target_artifacts / "pipeline_timing_summary.json"
        _write_json_atomic(path, summary)

    def _write_ocr_debug_artifacts(
        self, source: Path, image_id: str, source_regions: list[dict], groups: list[TextGroup], layout_graph: dict[str, Any],
    ) -> dict[str, str]:
        debug_dir = self.artifacts / image_id / "ocr_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as opened:
            image = opened.convert("RGB")
        overlay = image.copy()
        draw = ImageDraw.Draw(overlay)
        for index, region in enumerate(source_regions, 1):
            polygon = [[int(point[0]), int(point[1])] for point in region.get("polygon", [])]
            if len(polygon) >= 2:
                draw.line(polygon + [polygon[0]], fill=(220, 40, 40), width=2)
                x1, y1, _, _ = _box_from_polygon(polygon)
                draw.text((x1, max(0, y1 - 12)), f"r{index}", fill=(220, 40, 40))
        group_debug = []
        for index, group in enumerate(groups, 1):
            x1, y1, x2, y2 = [int(value) for value in group.bbox]
            draw.rectangle([x1, y1, x2, y2], outline=(40, 110, 230), width=3)
            draw.text((x1, y1), f"g{index}", fill=(40, 110, 230))
            pad = 8
            crop_box = (
                max(0, x1 - pad),
                max(0, y1 - pad),
                min(image.width, x2 + pad),
                min(image.height, y2 + pad),
            )
            crop_path = debug_dir / f"group_{index:03d}.png"
            if crop_box[2] > crop_box[0] and crop_box[3] > crop_box[1]:
                image.crop(crop_box).save(crop_path)
            group_debug.append({
                "index": index,
                "bbox": [x1, y1, x2, y2],
                "direction": group.direction,
                "text": group.text,
                "member_region_indices": group.member_indices,
                "crop": str(crop_path),
            })
        ocr_overlay = debug_dir / f"{source.stem}_ocr_overlay.png"
        overlay.save(ocr_overlay)
        reading_overlay = image.copy()
        reading_draw = ImageDraw.Draw(reading_overlay)
        nodes_by_id = {
            str(node.get("id")): node
            for node in layout_graph.get("nodes", [])
            if isinstance(node, dict)
        }
        for order, node_id in enumerate(layout_graph.get("reading_order", []), 1):
            node = nodes_by_id.get(str(node_id))
            if not node:
                continue
            x1, y1, x2, y2 = [int(value) for value in node.get("bbox", [0, 0, 0, 0])]
            reading_draw.rectangle([x1, y1, x2, y2], outline=(25, 160, 80), width=3)
            reading_draw.text((x1, max(0, y1 - 14)), str(order), fill=(25, 160, 80))
        reading_path = debug_dir / f"{source.stem}_reading_order_overlay.png"
        reading_overlay.save(reading_path)

        bubble_overlay = image.copy()
        bubble_draw = ImageDraw.Draw(bubble_overlay, "RGBA")
        safe_overlay = image.copy()
        safe_draw = ImageDraw.Draw(safe_overlay, "RGBA")
        for group in groups:
            x1, y1, x2, y2 = [int(value) for value in group.bbox]
            bubble_draw.rectangle([x1, y1, x2, y2], outline=(80, 150, 255, 220), width=3)
            bubble_draw.rectangle([x1, y1, x2, y2], fill=(80, 150, 255, 28))
            inset = max(3, int(min(max(1, x2 - x1), max(1, y2 - y1)) * 0.08))
            safe_draw.rectangle([x1 + inset, y1 + inset, x2 - inset, y2 - inset], outline=(255, 180, 40, 230), width=3)
            safe_draw.rectangle([x1 + inset, y1 + inset, x2 - inset, y2 - inset], fill=(255, 180, 40, 28))
        bubble_path = debug_dir / f"{source.stem}_bubble_mask_overlay.png"
        safe_path = debug_dir / f"{source.stem}_safe_area_overlay.png"
        bubble_overlay.save(bubble_path)
        safe_overlay.save(safe_path)
        (debug_dir / "groups.json").write_text(json.dumps(group_debug, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "ocr_overlay": str(ocr_overlay.resolve()),
            "bubble_mask_overlay": str(bubble_path.resolve()),
            "safe_area_overlay": str(safe_path.resolve()),
            "reading_order_overlay": str(reading_path.resolve()),
            "groups": str((debug_dir / "groups.json").resolve()),
        }

class PipelineService(QObject):
    progress = Signal(str, str, int, int, str)
    image_finished = Signal(str, object)
    image_failed = Signal(str, str)
    completed = Signal(bool)
    request_state_changed = Signal(str, str, str)
    scheduler_stats = Signal(object)

    def __init__(self, queue: TranslationQueue | None = None) -> None:
        super().__init__()
        self._queue = queue or TRANSLATION_QUEUE
        self._future = None
        self._worker: PipelineWorker | None = None
        self._request_prefix_by_image: dict[str, str] = {}
        self._active_request_images: set[str] = set()
        self._next_request_type: TranslationRequestType | None = None
        self._next_force_retranslate = False
        self._queue.failed.connect(self._on_queue_request_failed)
        if hasattr(self._queue, "state_changed"):
            self._queue.state_changed.connect(self._on_queue_state_changed)

    def set_request_type(self, request_type: TranslationRequestType | str) -> None:
        self._next_request_type = TranslationRequestType(request_type)

    def set_force_retranslate(self, force: bool) -> None:
        self._next_force_retranslate = bool(force)

    def _request_id(self, image_id: str) -> str:
        prefix = self._request_prefix_by_image.get(image_id, "batch")
        return f"{prefix}:{image_id}"

    @staticmethod
    def _task_status(stage: str) -> TranslationRequestStatus:
        return {
            "preprocessing": TranslationRequestStatus.OCR,
            "ocr": TranslationRequestStatus.OCR,
            "translating": TranslationRequestStatus.TRANSLATING,
            "rendering": TranslationRequestStatus.RENDERING,
            "reconstructing": TranslationRequestStatus.RENDERING,
        }.get(stage, TranslationRequestStatus.OCR)

    @Slot(str, str, int, int, str)
    def _on_worker_stage(self, image_id: str, stage: str, position: int, total: int, message: str) -> None:
        normalized = self._task_status(stage).value
        self.request_state_changed.emit(self._request_id(image_id), normalized, message)

    @Slot(str, object)
    def _on_worker_image_finished(self, image_id: str, result: dict) -> None:
        self.request_state_changed.emit(self._request_id(image_id), "done", "Done")
        self._active_request_images.discard(image_id)

    @Slot(str, str)
    def _on_worker_image_failed(self, image_id: str, message: str) -> None:
        if image_id:
            self.request_state_changed.emit(self._request_id(image_id), "failed", message)
            self._active_request_images.discard(image_id)
            return
        for active_image_id in tuple(self._active_request_images):
            self.request_state_changed.emit(
                self._request_id(active_image_id), "failed", message,
            )
        self._active_request_images.clear()

    @property
    def running(self) -> bool:
        return self._future is not None

    def _planned_project_items(
        self,
        project,
        image_ids: set[str] | None,
        config: dict[str, Any],
        *,
        force_retranslate: bool,
    ) -> list[dict[str, Any]]:
        eligible = {"pending", "queued", "failed", "cancelled"}
        manifest_path = target_manifest_path(
            project.artifacts,
            project.target_language,
        )
        legacy_manifest = project.artifacts / "chapter_job_manifest.json"
        manifest = JobManifest.load(
            manifest_path
            if manifest_path.is_file() or not legacy_manifest.is_file()
            else legacy_manifest
        )
        state_manager = _state_manager_for_stages(
            manifest,
            StageContract("translating"),
            StageContract("rendering", requires=("translating",)),
        )
        state_manager.validator.repair_stage_metadata_defaults()
        items: list[dict[str, Any]] = []
        repaired_project_state = False
        for image in project.images:
            if image_ids is not None and image.id not in image_ids:
                continue
            if force_retranslate:
                _clear_page_retranslate_artifacts(project, image)
                image.status = "queued"
                items.append(asdict(image))
                repaired_project_state = True
                continue
            if image.status in eligible:
                completed = _completed_project_output(project, image)
                if completed is not None:
                    for key, value in completed.items():
                        if getattr(image, key, None) != value:
                            setattr(image, key, value)
                            repaired_project_state = True
                    continue

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
            preview_path = render_dir / f"{source.stem}_preview.png"
            render_report_path = render_dir / f"{source.stem}_render.json"
            page = manifest.pages.get(image.id)
            translation_record = (
                page.stage_records.get("translating", {})
                if page is not None
                else {}
            )
            translation_input = str(
                translation_record.get("input_fingerprint", "")
            )
            ocr_path = project.artifacts / f"{image.id}_ocr.json"
            try:
                render_input = _render_stage_fingerprint(
                    source,
                    translation_path,
                    target=project.target_language,
                    config=config,
                )
            except (OSError, ValueError, TypeError):
                render_input = ""
            plan = state_manager.plan_page(
                image.id,
                source,
                {
                    "translating": StageValidationRequest(
                        input_fingerprint=translation_input,
                        artifacts={"translation_result": translation_path},
                        input_artifacts={"ocr_result": ocr_path},
                        source_path=source,
                        application_version=__version__,
                        settings_fingerprint=_translation_settings_fingerprint(
                            config,
                            project.target_language,
                        ),
                        provider_identity=_provider_identity(config),
                        model_identity=_model_identity(config),
                    ),
                    "rendering": StageValidationRequest(
                        input_fingerprint=render_input,
                        artifacts=_existing_artifacts(
                            rendered_image=final_path,
                            preview_image=preview_path,
                            render_report=render_report_path,
                        ),
                        input_artifacts={
                            "translation_result": translation_path,
                        },
                        source_path=source,
                        application_version=__version__,
                        settings_fingerprint=_render_settings_fingerprint(
                            config,
                            project.target_language,
                        ),
                    ),
                },
            )
            if plan.executable_stages:
                items.append(asdict(image))
        if repaired_project_state:
            project.save()
        return items

    def process_project(self, project, image_ids: set[str] | None = None) -> bool:
        if self.running:
            self._next_force_retranslate = False
            self._next_request_type = None
            return False
        force_retranslate = self._next_force_retranslate
        self._next_force_retranslate = False
        request_type = self._next_request_type or (
            TranslationRequestType.SELECTED
            if image_ids is not None
            else TranslationRequestType.BATCH
        )
        self._next_request_type = None
        prefix = request_type.value
        config = {
            "project_id": project.id,
            "source_language": project.source_language,
            "quality": project.quality,
            "literal_provider": project.literal_provider,
            "localization_provider": project.localization_provider,
            "localization_model": project.localization_model,
            "localization_style": project.localization_style,
            "text_style": project.text_style,
            "auto_fit": project.auto_fit,
            "bubble_padding": project.bubble_padding,
            "max_lines": project.max_lines,
            "glossary": project.glossary,
            "translation_engine": SETTINGS.translation_engine,
            "translation_fallback_engine": SETTINGS.translation_fallback_engine,
            # The fallback is an explicit user selection. Keep local providers
            # lazy, but allow Marian to be constructed if a cloud engine fails.
            "allow_local_fallback_for_cloud": True,
            "debug_artifacts_enabled": SETTINGS.debug_artifacts_enabled,
            "ocr_subprocess_enabled": SETTINGS.ocr_subprocess_enabled,
            "ocr_worker_recycle_pages": SETTINGS.ocr_worker_recycle_pages,
            "ocr_worker_memory_limit_mb": SETTINGS.ocr_worker_memory_limit_mb,
            "streaming_enabled": SETTINGS.streaming_enabled,
            "translation_concurrency": SETTINGS.translation_concurrency,
            "fast_worker_override": SETTINGS.fast_worker_override,
            "translation_memory_enabled": SETTINGS.translation_memory_enabled,
            "translation_memory_auto_learn": (
                SETTINGS.translation_memory_auto_learn
            ),
            "translation_memory_prefer_verified": (
                SETTINGS.translation_memory_prefer_verified
            ),
            "translate_title": SETTINGS.translate_titles,
            "translate_sfx": SETTINGS.translate_sfx,
            "translate_sign": SETTINGS.translate_signs,
            "translate_credit": SETTINGS.translate_credits,
            "qwen_model_path": SETTINGS.qwen_model_path,
            "qwen_model_name": SETTINGS.qwen_model_name,
            "provider_models": {
                "groq": SETTINGS.groq_model,
                "gemini": SETTINGS.gemini_model,
                "deepseek": SETTINGS.deepseek_model,
                "openai": SETTINGS.openai_model,
                "openai_compatible": SETTINGS.openai_compatible_model,
            },
            "provider_base_urls": {
                "openai_compatible": SETTINGS.openai_compatible_base_url,
            },
            "force_retranslate": force_retranslate,
        }
        items = self._planned_project_items(
            project,
            image_ids,
            config,
            force_retranslate=force_retranslate,
        )
        if not items:
            return False
        self._request_prefix_by_image = {
            str(item["id"]): prefix for item in items
        }
        self._active_request_images = set(self._request_prefix_by_image)
        for item in items:
            self.request_state_changed.emit(
                self._request_id(str(item["id"])), "queued", "Queued",
            )
        image_positions = {
            image.id: index for index, image in enumerate(project.images)
        }
        requests = tuple(
            TranslationRequest(
                request_id=self._request_id(str(item["id"])),
                type=request_type,
                project_id=project.id,
                image_id=str(item["id"]),
                image_index=image_positions[str(item["id"])],
                source_path=Path(item["source_path"]),
                target_language=project.target_language,
                source_language=project.source_language,
                metadata={"item": dict(item)},
            )
            for item in items
        )
        self._future = self._queue.submit_group(
            requests,
            lambda grouped, token, group_progress: self._run_request_group(
                grouped,
                token,
                group_progress,
                items=items,
                artifacts=project.artifacts,
                target=project.target_language,
                config=config,
            ),
        )
        return True

    def cancel(self) -> None:
        if not self._active_request_images:
            return
        image_id = next(iter(self._active_request_images))
        self._queue.cancel(self._request_id(image_id))

    def _run_request_group(
        self,
        requests: tuple[TranslationRequest, ...],
        token: CancellationToken,
        group_progress,
        *,
        items: list[dict],
        artifacts: Path,
        target: str,
        config: dict,
    ) -> dict[str, None]:
        request_id_by_image = {
            request.image_id: request.request_id for request in requests
        }
        worker = PipelineWorker(items, artifacts, target, token.event, config)
        self._worker = worker
        cancelled: list[bool] = []

        def safe_group_progress(
            request_id: str,
            status: TranslationRequestStatus,
            message: str = "",
        ) -> None:
            try:
                group_progress(request_id, status, message)
            except RequestCancelled:
                if not token.requested:
                    raise

        def queue_stage(
            image_id: str,
            stage: str,
            position: int,
            total: int,
            message: str,
        ) -> None:
            safe_group_progress(
                request_id_by_image[image_id],
                self._task_status(stage),
                message,
            )

        def queue_finished(image_id: str, result: dict) -> None:
            safe_group_progress(
                request_id_by_image[image_id],
                TranslationRequestStatus.DONE,
                "Done",
            )

        def queue_failed(image_id: str, message: str) -> None:
            target_ids = (
                (request_id_by_image[image_id],)
                if image_id in request_id_by_image
                else tuple(request_id_by_image.values())
            )
            for request_id in target_ids:
                safe_group_progress(
                    request_id,
                    TranslationRequestStatus.FAILED,
                    message,
                )

        worker.stage.connect(queue_stage, Qt.ConnectionType.DirectConnection)
        worker.stage.connect(self.progress)
        worker.stage.connect(self._on_worker_stage)
        if hasattr(worker, "scheduler_snapshot"):
            worker.scheduler_snapshot.connect(self.scheduler_stats)
        worker.image_finished.connect(queue_finished, Qt.ConnectionType.DirectConnection)
        worker.image_finished.connect(self.image_finished)
        worker.image_finished.connect(self._on_worker_image_finished)
        worker.image_failed.connect(queue_failed, Qt.ConnectionType.DirectConnection)
        worker.image_failed.connect(self.image_failed)
        worker.image_failed.connect(self._on_worker_image_failed)
        worker.finished.connect(cancelled.append, Qt.ConnectionType.DirectConnection)
        worker.finished.connect(self._finish)
        worker.run()
        if cancelled and cancelled[-1]:
            token.cancel()
            token.raise_if_cancelled()
        return {request.request_id: None for request in requests}

    @Slot(str, object)
    def _on_queue_request_failed(self, request_id: str, result: dict) -> None:
        image_id = next(
            (
                image_id for image_id in self._active_request_images
                if self._request_id(image_id) == request_id
            ),
            None,
        )
        if image_id is None:
            return
        message = str(result.get("message") or "Pipeline request failed")
        self.image_failed.emit(image_id, message)
        self._on_worker_image_failed(image_id, message)
        if not self._active_request_images and self.running:
            self._finish(False)

    @Slot(str, str, str)
    def _on_queue_state_changed(
        self,
        request_id: str,
        status: str,
        message: str,
    ) -> None:
        if status != TranslationRequestStatus.CANCELLED.value or not self.running:
            return
        if not any(
            self._request_id(image_id) == request_id
            for image_id in self._active_request_images
        ):
            return
        self._finish(True)

    @Slot(bool)
    def _finish(self, cancelled: bool) -> None:
        if cancelled:
            for image_id in tuple(self._active_request_images):
                self.request_state_changed.emit(
                    self._request_id(image_id), "cancelled", "Cancelled",
                )
        self._active_request_images.clear()
        self._future = None
        self._worker = None
        self.completed.emit(cancelled)
        self._request_prefix_by_image.clear()
