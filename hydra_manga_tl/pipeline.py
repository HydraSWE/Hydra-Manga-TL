"""Cancellable background pipeline using the validated OCR/translation/render engines."""

from __future__ import annotations

import json
import gc
import hashlib
import re
import threading
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from PySide6.QtCore import QObject, Qt, Signal, Slot

from .context_engine import ContextEngine
from .intelligent_page import IntelligentPageResult
from .job_manifest import JobManifest
from .language import resolve_source_language
from .layout import TextGroup, classify_text_group, group_regions
from .layout_graph import build_layout_graph
from .ocr import (
    FULL_PAGE_OCR_MAX_SIDE,
    OCRResult,
)
from .ocr_service import OCRService, current_rss_mb
from .phase3_cli import run as render_phase3
from .paths import PATHS
from .preprocessor import prepare_ocr_image
from .review import review_translation_groups
from .segmentation import segment_bubble
from .settings import SETTINGS
from .stage_streaming import BoundedStageExecutor
from .typesetting import review_rendered_group, summarize_render_review
from .render_queue import RENDER_QUEUE
from .translation_engines import PageDialogue, PageTranslation
from .translation_requests import RenderRequest
from .translation_requests import TranslationRequest, TranslationRequestStatus, TranslationRequestType
from .translation_cache_store import TRANSLATION_CACHE
from .translation_queue import CancellationToken, RequestCancelled, TRANSLATION_QUEUE, TranslationQueue
from .translation_runtime import TRANSLATION_RUNTIME


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


def _box_from_polygon(polygon: list[list[int]]) -> tuple[int, int, int, int]:
    xs = [int(point[0]) for point in polygon]
    ys = [int(point[1]) for point in polygon]
    return min(xs), min(ys), max(xs), max(ys)


class PipelineWorker(QObject):
    stage = Signal(str, str, int, int, str)
    image_finished = Signal(str, object)
    image_failed = Signal(str, str)
    finished = Signal(bool)

    def __init__(self, items: list[dict], artifacts: Path, target: str, cancel: threading.Event, config: dict | None = None) -> None:
        super().__init__()
        self.items = items
        self.artifacts = artifacts
        self.target = target
        self.cancel = cancel
        self.config = config or {}

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
        return TRANSLATION_RUNTIME.translate_page(page, self.config)

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
            "pipeline_version": "0.8.0-alpha",
            "source": str(source.resolve()),
            "source_language": source_language,
            "target_language": self.target,
            "ocr_model_language": ocr_result.model_language,
            "source_regions": source_regions,
            "translation_groups": translated_groups,
            "preprocessing": preprocessed.to_dict(),
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

    @staticmethod
    def _render_translation_payload(request: RenderRequest):
        return RENDER_QUEUE.submit(
            request,
            lambda result_path, output_dir, policy: render_phase3(
                result_path, output_dir, policy=policy,
            ),
        ).result()["output"]

    @Slot()
    def run(self) -> None:
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
            job_manifest = JobManifest.load(self.artifacts / "chapter_job_manifest.json")
            
            for position, item in enumerate(self.items, 1):
                image_id, source = item["id"], Path(item["source_path"])
                job_manifest.ensure_page(image_id, str(source))
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
                try:
                    job_manifest.mark(image_id, "preprocessing")
                    self.stage.emit(image_id, "preprocessing", position, total, f"Preparing {source.name}")
                    stage_started = time.perf_counter()
                    ocr_path = self.artifacts / f"{image_id}_ocr.json"
                    preprocessed = prepare_ocr_image(source, self.artifacts / image_id / "preprocess")
                    source_for_ocr = Path(preprocessed.ocr_path)
                    timing["stages"]["preprocess_seconds"] = round(time.perf_counter() - stage_started, 3)
                    timing["preprocessing"] = preprocessed.to_dict()
                    if self.cancel.is_set():
                        cancelled = True
                        break

                    job_manifest.mark(image_id, "OCR", stage="preprocessing")
                    self.stage.emit(image_id, "ocr", position, total, f"Reading text in {source.name}")
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
                    service_result = self._run_ocr_page(
                        ocr_service,
                        source_for_ocr,
                        preferred_language=cache_preferred,
                        quality=quality,
                        auto_language_fallback=quality == "Maximum" and requested_source == "auto" and chapter_ocr_language is not None,
                        cache_path=ocr_cache_path,
                        checkpoint_path=ocr_path,
                    )
                    ocr_result = service_result.ocr_result
                    source_regions = service_result.final_regions
                    timing["cache"]["ocr_hit"] = service_result.cache_hit
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
                    if self.cancel.is_set():
                        cancelled = True
                        break

                    job_manifest.mark(image_id, "translating", stage="OCR")
                    self.stage.emit(image_id, "translating", position, total, f"Translating {source.name}")
                    stage_started = time.perf_counter()
                    with Image.open(source) as opened:
                        page_size = opened.size
                    layout_graph = build_layout_graph(source_regions, page_size=page_size)
                    groups = group_regions(source_regions)
                    group_payloads = []
                    group_source_polygons = []
                    
                    for group in groups:
                        text = group.text
                        placement_bbox = group.bbox
                        confidence = min(source_regions[index - 1]["confidence"] for index in group.member_indices)
                        polygons = [source_regions[index - 1]["polygon"] for index in group.member_indices]

                        effective_group = TextGroup(group.member_indices, text, placement_bbox, group.direction)
                        bubble_type = _classify_bubble(effective_group)
                        classification = classify_text_group(effective_group, source_regions, ocr_result.model_language)
                        if bubble_type not in {"credit", "sfx"}:
                            bubble_type = classification.kind

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
                        })
                        group_source_polygons.append(polygons)

                    debug_artifacts = (
                        self._write_ocr_debug_artifacts(source, image_id, source_regions, groups, layout_graph.to_dict())
                        if bool(self.config.get("debug_artifacts_enabled", False))
                        else {}
                    )
                    timing["counts"]["source_regions"] = len(source_regions)
                    timing["counts"]["groups"] = len(groups)
                    timing["counts"]["layout_edges"] = len(layout_graph.edges)

                    # Filter dialogue array using the payload dictionary type
                    dialogue = []
                    for idx, item in enumerate(group_payloads):
                        if item["type"] not in ["dialogue", "narration"]:
                            continue
                        dialogue.append({
                            "id": f"r{idx + 1}",
                            "text": item["text"],
                            "confidence": item.get("confidence", 1.0),
                            "source_text": str(groups[idx].text).strip() if idx < len(groups) else "",
                            "reading_order": len(dialogue) + 1,
                            "source_direction": groups[idx].direction if idx < len(groups) else "",
                            "bbox": item.get("polygon", []),
                        })

                    page_context = context_engine.page_context(dialogue, layout_graph.to_dict(), position)

                    page = self._build_page_dialogue(
                        source_language, self.target, dialogue, page_context,
                    )

                    translation_cache_dir = PATHS.page_translation_cache
                    translation_cache_dir.mkdir(parents=True, exist_ok=True)
                    page_cache_key = _page_translation_cache_key(page, self.config, self.target)
                    page_cache_path = translation_cache_dir / f"{_cache_fragment(source.stem)}_{page_cache_key[:16]}.json"
                    precomputed_segmentations = None
                    if page_cache_path.exists():
                        page_payload = json.loads(page_cache_path.read_text(encoding="utf-8"))
                        page_result = PageTranslation(
                            source_language=str(page_payload.get("source_language", page.source_language)),
                            target_language=str(page_payload.get("target_language", page.target_language)),
                            translations=list(page_payload.get("translations", [])),
                        )
                        timing["cache"]["translation_hit"] = True
                    else:
                        translation_future = translation_pool.submit(
                            self._translate_page_dialogue, page,
                        )
                        if bool(self.config.get("streaming_enabled", True)):
                            precomputed_segmentations = self._segment_groups(
                                source, image_id, groups, group_payloads,
                            )
                        page_result = translation_future.result()
                        _write_json_atomic(page_cache_path, asdict(page_result))
                    timing["stages"]["translate_seconds"] = round(time.perf_counter() - stage_started, 3)

                    translated_map = {str(item.get("id")): str(item.get("text", "")) for item in page_result.translations}
                    
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
                    
                    NAME_LOCKS = {
                        "大和": "Yamato",
                    }
                    for idx, (group, payload) in enumerate(zip(groups, group_payloads)):
                        if payload["type"] in ["dialogue", "narration"] and group.text.strip() in NAME_LOCKS:
                            translated_map[f"r{idx + 1}"] = NAME_LOCKS[group.text.strip()]

                    translated_groups = []
                    bubble_segmentations = precomputed_segmentations or self._segment_groups(
                        source, image_id, groups, group_payloads,
                    )
                    
                    for idx, (group, polygons, payload, segmentation_payload) in enumerate(zip(
                        groups, group_source_polygons, group_payloads, bubble_segmentations,
                    )):
                        region_index = idx + 1
                        bubble_id = f"r{region_index}"
                        
                        if payload["type"] not in ["dialogue", "narration"]:
                            translated_text = ""
                            status = "preserved" 
                            confidence_score = 1.0
                            review_reasons = list(payload.get("classification_reasons", [])) or [f"{payload['type']}_preserved"]
                        else:
                            translated_text = translated_map.get(bubble_id, "").strip()
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
                            "type": payload.get("type", "dialogue"),
                            "bubble_type": payload.get("type", "dialogue"),
                            "ocr_confidence": float(min(source_regions[i - 1]["confidence"] for i in group.member_indices)),
                            "polygon": payload.get("polygon", [[group.bbox[0], group.bbox[1]], [group.bbox[2], group.bbox[1]], [group.bbox[2], group.bbox[3]], [group.bbox[0], group.bbox[3]]]),
                            "status": status,
                            "review_reasons": list(dict.fromkeys(review_reasons)),
                            "alternatives": [],
                            "provider": TRANSLATION_RUNTIME.last_engine_id if not timing["cache"]["translation_hit"] else "page-cache",
                            "model": "",
                            "localization_style": self.config.get("localization_style", "Manga"),
                            "translation_quality": "good" if status == "translated" else "review",
                            "localization_note": "",
                            "member_region_indices": group.member_indices,
                            "source_direction": group.direction,
                            "direction": "horizontal-ltr",
                            "source_polygons": polygons,
                            "source_member_texts": [str(source_regions[i - 1].get("text", "")) for i in group.member_indices],
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
                    translation_path = self.artifacts / f"{image_id}_translated_{self.target}.json"
                    _write_json_atomic(translation_path, phase2)
                    if self.cancel.is_set():
                        cancelled = True
                        break

                    job_manifest.mark(image_id, "rendering", stage="translating")
                    self.stage.emit(image_id, "reconstructing", position, total, f"Rebuilding {source.name}")
                    stage_started = time.perf_counter()
                    render_dir = self.artifacts / image_id
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
                    phase2["translation_groups"] = translated_groups
                    phase2["render_review"] = render_review
                    phase2["ai_review"] = ai_review
                    _write_json_atomic(translation_path, phase2)
                    timing["total_seconds"] = round(time.perf_counter() - image_started, 3)
                    timing["rss_after_page_mb"] = current_rss_mb()
                    timing["ocr_worker_restart_count"] = ocr_service.restart_count
                    timing["paths"] = {
                        "ocr_result": str(ocr_path),
                        "translation_result": str(translation_path),
                        "render_dir": str(render_dir),
                        "timing": str(self.artifacts / f"{image_id}_timing.json"),
                        "intelligent_page": str(self.artifacts / f"{image_id}_intelligent_page.json"),
                    }
                    intelligent = IntelligentPageResult(
                        pipeline_version="0.8.0-alpha",
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
                    _write_json_atomic(self.artifacts / f"{image_id}_intelligent_page.json", intelligent.to_dict())
                    job_manifest.mark(image_id, "review", stage="rendering")
                    self._write_image_timing(image_id, timing)
                    batch_timings.append(timing)
                    final_path = render_dir / f"{source.stem}_translated_en.png"
                    preview_path = render_dir / f"{source.stem}_preview.png"
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
        path = self.artifacts / f"{image_id}_timing.json"
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
        path = self.artifacts / "pipeline_timing_summary.json"
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

    def __init__(self, queue: TranslationQueue | None = None) -> None:
        super().__init__()
        self._queue = queue or TRANSLATION_QUEUE
        self._future = None
        self._worker: PipelineWorker | None = None
        self._request_prefix_by_image: dict[str, str] = {}
        self._active_request_images: set[str] = set()
        self._next_request_type: TranslationRequestType | None = None
        self._queue.failed.connect(self._on_queue_request_failed)
        if hasattr(self._queue, "state_changed"):
            self._queue.state_changed.connect(self._on_queue_state_changed)

    def set_request_type(self, request_type: TranslationRequestType | str) -> None:
        self._next_request_type = TranslationRequestType(request_type)

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

    def process_project(self, project, image_ids: set[str] | None = None) -> bool:
        if self.running:
            return False
        items = [
            asdict(item) for item in project.images
            if item.status in {"pending", "queued", "partial", "failed", "cancelled"}
            and (image_ids is None or item.id in image_ids)
        ]
        if not items:
            return False
        request_type = self._next_request_type or (
            TranslationRequestType.SELECTED
            if image_ids is not None
            else TranslationRequestType.BATCH
        )
        self._next_request_type = None
        prefix = request_type.value
        self._request_prefix_by_image = {
            str(item["id"]): prefix for item in items
        }
        self._active_request_images = set(self._request_prefix_by_image)
        for item in items:
            self.request_state_changed.emit(
                self._request_id(str(item["id"])), "queued", "Queued",
            )
        config = {
            "project_id": project.id,
            "source_language": project.source_language,
            "quality": project.quality,
            "literal_provider": project.literal_provider,
            "localization_provider": project.localization_provider,
            "localization_model": project.localization_model,
            "localization_style": project.localization_style,
            "text_style": project.text_style,
            "bubble_padding": project.bubble_padding,
            "max_lines": project.max_lines,
            "glossary": project.glossary,
            "translation_engine": SETTINGS.translation_engine,
            "translation_fallback_engine": SETTINGS.translation_fallback_engine,
            "debug_artifacts_enabled": SETTINGS.debug_artifacts_enabled,
            "ocr_subprocess_enabled": SETTINGS.ocr_subprocess_enabled,
            "ocr_worker_recycle_pages": SETTINGS.ocr_worker_recycle_pages,
            "ocr_worker_memory_limit_mb": SETTINGS.ocr_worker_memory_limit_mb,
            "streaming_enabled": SETTINGS.streaming_enabled,
            "translation_concurrency": SETTINGS.translation_concurrency,
            "qwen_model_path": SETTINGS.qwen_model_path,
            "qwen_model_name": SETTINGS.qwen_model_name,
            "provider_models": {
                "groq": SETTINGS.groq_model,
                "gemini": SETTINGS.gemini_model,
            },
        }
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
