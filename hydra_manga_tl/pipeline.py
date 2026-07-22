"""Cancellable background pipeline using the validated OCR/translation/render engines."""

from __future__ import annotations

import json
import hashlib
import re
import threading
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .language import resolve_source_language
from .layout import TextGroup, classify_text_group, compose_manual_region, group_regions
from .ocr import (
    FULL_PAGE_OCR_MAX_SIDE,
    OCRResult,
    normalize_ocr_text,
    ocr_text_quality,
    retry_suspicious_digit_regions,
    retry_short_vertical_regions,
)
from .ocr_runtime import get_ocr_engine
from .phase3_cli import run as render_phase3
from .settings import SETTINGS
from .translation_engines import PageDialogue, PageTranslation, TranslationEngineManager


def _needs_ocr_retry(text: str, confidence: float) -> bool:
    """Smart OCR Retry using length, digit ratio, and artifacts."""
    if confidence < 0.70: 
        return True
    
    digit_count = sum(c.isdigit() for c in text)
    if len(text) > 0 and (digit_count / len(text)) > 0.4: 
        return True
        
    stripped = text.strip(".,!?~- ")
    if len(text) > 2 and len(stripped) == 0: 
        return True
        
    if re.search(r'[@#$%^&*]', text): 
        return True
        
    return False


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
    return _stable_json_hash({
        "kind": "page-translation-v1",
        "source_language": page.source_language,
        "target_language": target,
        "dialogue": page.dialogue,
        "page_context": page.page_context,
        "glossary": config.get("glossary", {}),
        "engine": config.get("translation_engine", "qwen"),
        "qwen_model": config.get("qwen_model_path") or config.get("qwen_model") or config.get("qwen_model_name", ""),
        "localization_style": config.get("localization_style", "Manga"),
    })


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
            ocr_engine = get_ocr_engine(_ocr_engine_languages(requested_source, quality, preferred))
            chapter_ocr_language = preferred
            total = len(self.items)
            batch_started = time.perf_counter()
            batch_timings: list[dict[str, Any]] = []
            translation_manager: TranslationEngineManager | None = None
            
            for position, item in enumerate(self.items, 1):
                image_id, source = item["id"], Path(item["source_path"])
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
                        "short_vertical_pass": False,
                        "suspicious_member_groups": 0,
                        "focused_group_retries": 0,
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
                    self.stage.emit(image_id, "ocr", position, total, f"Reading text in {source.name}")
                    stage_started = time.perf_counter()
                    ocr_cache_dir = self.artifacts / "ocr_cache"
                    ocr_cache_dir.mkdir(parents=True, exist_ok=True)
                    cache_preferred = None if quality == "Maximum" else chapter_ocr_language
                    ocr_cache_key = _ocr_cache_key(source, requested_source, quality, cache_preferred)
                    ocr_cache_path = ocr_cache_dir / f"{_cache_fragment(source.stem)}_{ocr_cache_key[:16]}.json"
                    if ocr_cache_path.exists():
                        ocr_result = OCRResult.from_dict(json.loads(ocr_cache_path.read_text(encoding="utf-8")))
                        ocr_result.metadata = {**ocr_result.metadata, "cache_hit": True, "cache_path": str(ocr_cache_path)}
                        timing["cache"]["ocr_hit"] = True
                        page_ocr_engine = ocr_engine
                    else:
                        page_ocr_engine = ocr_engine
                        ocr_result = page_ocr_engine.analyze(source, cache_preferred)
                        if requested_source == "auto" and chapter_ocr_language is not None and _needs_auto_ocr_fallback(ocr_result):
                            timing["retry"]["auto_language_fallback"] = True
                            fallback_engine = get_ocr_engine(("japan", "ch", "en"))
                            page_ocr_engine = fallback_engine
                            ocr_result = fallback_engine.analyze(source)
                        ocr_result.metadata = {**ocr_result.metadata, "cache_hit": False, "cache_path": str(ocr_cache_path)}
                        ocr_cache_path.write_text(json.dumps(ocr_result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
                    timing["stages"]["ocr_seconds"] = round(time.perf_counter() - stage_started, 3)
                    if requested_source == "auto":
                        chapter_ocr_language = ocr_result.model_language
                    source_language = resolve_source_language(requested_source, ocr_result.language)
                    ocr_path = self.artifacts / f"{image_id}_ocr.json"
                    ocr_path.write_text(json.dumps(ocr_result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
                    if self.cancel.is_set():
                        cancelled = True
                        break

                    # Retry vertical regions with suspiciously short text (missing columns)
                    if quality != "Fast":
                        retry_started = time.perf_counter()
                        timing["retry"]["short_vertical_pass"] = True
                        source_regions_dict = ocr_result.to_dict()["regions"]
                        source_regions_dict = retry_short_vertical_regions(
                            page_ocr_engine, source, source_regions_dict, ocr_result.model_language,
                        )
                        timing["stages"]["short_vertical_retry_seconds"] = round(time.perf_counter() - retry_started, 3)
                    else:
                        source_regions_dict = ocr_result.to_dict()["regions"]

                    # Use the (possibly expanded) region list for all subsequent operations
                    source_regions = source_regions_dict

                    self.stage.emit(image_id, "translating", position, total, f"Translating {source.name}")
                    stage_started = time.perf_counter()
                    groups = group_regions(source_regions)
                    group_payloads = []
                    group_source_polygons = []
                    
                    for group in groups:
                        text = group.text
                        original_had_digits = any(char.isascii() and char.isdigit() for char in text)
                        placement_bbox = group.bbox
                        confidence = min(source_regions[index - 1]["confidence"] for index in group.member_indices)
                        polygons = [source_regions[index - 1]["polygon"] for index in group.member_indices]
                        
                        if quality != "Fast":
                            timing["retry"]["suspicious_member_groups"] += 1
                            text, confidence, polygons = self._retry_suspicious_members(
                                page_ocr_engine, source, group.member_indices, source_regions, ocr_result.model_language,
                            )
                        
                        focused_retry_fixed_digits = original_had_digits and not any(char.isascii() and char.isdigit() for char in text)
                        
                        if quality != "Fast" and not focused_retry_fixed_digits and _needs_ocr_retry(text, confidence):
                            timing["retry"]["focused_group_retries"] += 1
                            retry = page_ocr_engine.analyze_selection(
                                source, group.bbox, preferred_language=ocr_result.model_language, add_context=True,
                                rtl_context=group.direction == "vertical-rtl",
                            )
                            retry_regions = retry.to_dict()["regions"]
                            if retry_regions:
                                retry_regions = self._merge_retry_regions(
                                    [source_regions[index - 1] for index in group.member_indices], retry_regions,
                                )
                                retry_group = compose_manual_region(retry_regions)
                                retry_confidence = min(region["confidence"] for region in retry_regions)
                                if self._ocr_group_quality(retry_group.text, retry_confidence, ocr_result.model_language) > self._ocr_group_quality(text, confidence, ocr_result.model_language) + 0.02:
                                    text = retry_group.text
                                    confidence = retry_confidence
                                    polygons = [region["polygon"] for region in retry_regions]
                                    placement_bbox = retry_group.bbox

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
                            "polygon": [[placement_bbox[0], placement_bbox[1]], [placement_bbox[2], placement_bbox[1]], [placement_bbox[2], placement_bbox[3]], [placement_bbox[0], placement_bbox[3]]],
                        })
                        group_source_polygons.append(polygons)

                    self._write_ocr_debug_artifacts(source, image_id, source_regions, groups)
                    timing["counts"]["source_regions"] = len(source_regions)
                    timing["counts"]["groups"] = len(groups)

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

                    has_names = bool(self.config.get("glossary"))
                    has_dialogue = any(p["type"] == "dialogue" for p in group_payloads)
                    has_narration = any(p["type"] == "narration" for p in group_payloads)
                    
                    page_context = (
                        f"Translate this manga page. Total text units to process: {len(dialogue)}. "
                        "Reading direction: right-to-left. "
                    )
                    if has_dialogue: page_context += "Contains dialogue. "
                    if has_narration: page_context += "Contains narration. "
                    if has_names: page_context += "Use the provided glossary strictly to maintain character name consistency. "
                    page_context += (
                        "Keep one translation per bubble. If a short CJK-only block looks like a character name, preserve it. "
                        "Use reading_order and bbox to infer nearby speaker/target relationships. "
                        "When a Japanese line omits the subject, prefer the previously addressed listener for body/action descriptions unless a first-person marker is present."
                    )

                    page = PageDialogue(
                        source_language=source_language,
                        target_language=self.target,
                        dialogue=dialogue,
                        page_context=page_context,
                    )

                    translation_cache_dir = self.artifacts / "page_translation_cache"
                    translation_cache_dir.mkdir(parents=True, exist_ok=True)
                    page_cache_key = _page_translation_cache_key(page, self.config, self.target)
                    page_cache_path = translation_cache_dir / f"{_cache_fragment(source.stem)}_{page_cache_key[:16]}.json"
                    if page_cache_path.exists():
                        page_payload = json.loads(page_cache_path.read_text(encoding="utf-8"))
                        page_result = PageTranslation(
                            source_language=str(page_payload.get("source_language", page.source_language)),
                            target_language=str(page_payload.get("target_language", page.target_language)),
                            translations=list(page_payload.get("translations", [])),
                        )
                        timing["cache"]["translation_hit"] = True
                    else:
                        if translation_manager is None:
                            translation_manager = TranslationEngineManager(
                                glossary=self.config.get("glossary", {}),
                                qwen_model_path=self.config.get("qwen_model_path") or self.config.get("qwen_model"),
                                preferred_engine=self.config.get("translation_engine", "qwen"),
                                qwen_model_name=self.config.get("qwen_model_name", "Qwen3-4B-Instruct-2507"),
                            )
                            translation_manager.load()
                        page_result = translation_manager.translate_page(page)
                        page_cache_path.write_text(json.dumps(asdict(page_result), ensure_ascii=False, indent=2), encoding="utf-8")
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
                    
                    for idx, (group, polygons, payload) in enumerate(zip(groups, group_source_polygons, group_payloads)):
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
                            if confidence_score <= 0.5:
                                review_reasons.append("low_confidence_or_empty")
                            status = "review" if review_reasons else "translated"
                            
                        translated_groups.append({
                            "index": region_index,
                            "original_text": str(group.text).strip(),
                            "literal_text": str(group.text).strip(),
                            "translated_text": translated_text,
                            "ocr_confidence": float(min(source_regions[i - 1]["confidence"] for i in group.member_indices)),
                            "polygon": payload.get("polygon", [[group.bbox[0], group.bbox[1]], [group.bbox[2], group.bbox[1]], [group.bbox[2], group.bbox[3]], [group.bbox[0], group.bbox[3]]]),
                            "status": status,
                            "review_reasons": list(dict.fromkeys(review_reasons)),
                            "alternatives": [],
                            "provider": "qwen-gguf-or-marian",
                            "model": "",
                            "localization_style": self.config.get("localization_style", "Manga"),
                            "translation_quality": "good" if status == "translated" else "review",
                            "localization_note": "",
                            "member_region_indices": group.member_indices,
                            "source_direction": group.direction,
                            "direction": "horizontal-ltr",
                            "source_polygons": polygons,
                            "source_member_texts": [str(source_regions[i - 1].get("text", "")) for i in group.member_indices],
                        })

                    phase2 = {
                        "source": str(source.resolve()), "source_language": source_language,
                        "target_language": self.target, "ocr_model_language": ocr_result.model_language,
                        "source_regions": source_regions, "translation_groups": translated_groups,
                        "literal_provider": self.config.get("literal_provider", "marian"),
                        "localization_provider": self.config.get("localization_provider", "local"),
                        "localization_style": self.config.get("localization_style", "Manga"),
                        "text_style": self.config.get("text_style", "Manga"),
                        "bubble_padding": int(self.config.get("bubble_padding", 5)),
                        "max_lines": int(self.config.get("max_lines", 3)),
                    }
                    translation_path = self.artifacts / f"{image_id}_translated_{self.target}.json"
                    translation_path.write_text(json.dumps(phase2, ensure_ascii=False, indent=2), encoding="utf-8")
                    if self.cancel.is_set():
                        cancelled = True
                        break

                    self.stage.emit(image_id, "reconstructing", position, total, f"Rebuilding {source.name}")
                    stage_started = time.perf_counter()
                    render_dir = self.artifacts / image_id
                    render_phase3(translation_path, render_dir, policy="complete")
                    timing["stages"]["render_seconds"] = round(time.perf_counter() - stage_started, 3)
                    timing["total_seconds"] = round(time.perf_counter() - image_started, 3)
                    timing["paths"] = {
                        "ocr_result": str(ocr_path),
                        "translation_result": str(translation_path),
                        "render_dir": str(render_dir),
                        "timing": str(self.artifacts / f"{image_id}_timing.json"),
                    }
                    self._write_image_timing(image_id, timing)
                    batch_timings.append(timing)
                    final_path = render_dir / f"{source.stem}_translated_en.png"
                    preview_path = render_dir / f"{source.stem}_preview.png"
                    review = any(group["status"] == "review" for group in translated_groups)
                    self.image_finished.emit(image_id, {
                        "status": "review" if review else "ready", "source_language": source_language,
                        "ocr_result": str(ocr_path), "translation_result": str(translation_path),
                        "rendered_image": str(final_path), "preview_image": str(preview_path), "error": "",
                    })
                except Exception as error:
                    timing["error"] = f"{type(error).__name__}: {error}"
                    timing["traceback"] = traceback.format_exc()
                    timing["total_seconds"] = round(time.perf_counter() - image_started, 3)
                    self._write_image_timing(image_id, timing)
                    batch_timings.append(timing)
                    self.image_failed.emit(image_id, f"{type(error).__name__}: {error}")
        except Exception as error:
            self.image_failed.emit("", f"Pipeline initialization failed: {type(error).__name__}: {error}")
        finally:
            if "translation_manager" in locals() and translation_manager is not None:
                translation_manager.unload()
            if "batch_timings" in locals():
                self._write_batch_timing(batch_timings, time.perf_counter() - batch_started)
        self.finished.emit(cancelled)

    def _write_image_timing(self, image_id: str, timing: dict[str, Any]) -> None:
        path = self.artifacts / f"{image_id}_timing.json"
        path.write_text(json.dumps(timing, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_batch_timing(self, image_timings: list[dict[str, Any]], elapsed_seconds: float) -> None:
        summary = {
            "total_seconds": round(elapsed_seconds, 3),
            "image_count": len(image_timings),
            "ocr_cache_hits": sum(1 for item in image_timings if item.get("cache", {}).get("ocr_hit")),
            "translation_cache_hits": sum(1 for item in image_timings if item.get("cache", {}).get("translation_hit")),
            "images": image_timings,
        }
        path = self.artifacts / "pipeline_timing_summary.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_ocr_debug_artifacts(self, source: Path, image_id: str, source_regions: list[dict], groups: list[TextGroup]) -> None:
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
        overlay.save(debug_dir / f"{source.stem}_ocr_overlay.png")
        (debug_dir / "groups.json").write_text(json.dumps(group_debug, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _ocr_group_quality(text: str, confidence: float, model_language: str) -> float:
        return ocr_text_quality(text, confidence, model_language)

    @classmethod
    def _retry_suspicious_members(cls, ocr_engine, source: Path, member_indices: list[int], source_regions: list[dict], model_language: str):
        entries = retry_suspicious_digit_regions(
            ocr_engine, source, [source_regions[index - 1] for index in member_indices], model_language,
        )
        return (
            "".join(str(entry["text"]) for entry in entries),
            min(float(entry["confidence"]) for entry in entries),
            [entry["polygon"] for entry in entries],
        )

    @staticmethod
    def _normalize_ocr_text(text: str) -> str:
        return normalize_ocr_text(text)

    @classmethod
    def _merge_retry_regions(cls, originals: list[dict], retries: list[dict]) -> list[dict]:
        merged = [{**region, "text": cls._normalize_ocr_text(str(region["text"]))} for region in retries]
        for original in originals:
            if float(original["confidence"]) < 0.80:
                continue
            match = next((index for index, retry in enumerate(merged) if cls._polygon_overlap(original["polygon"], retry["polygon"]) >= 0.35), None)
            if match is None:
                merged.append({**original})
            elif float(original["confidence"]) >= float(merged[match]["confidence"]):
                merged[match] = {**original}
        return merged

    @staticmethod
    def _polygon_overlap(first: list[list[int]], second: list[list[int]]) -> float:
        first_x, first_y = [point[0] for point in first], [point[1] for point in first]
        second_x, second_y = [point[0] for point in second], [point[1] for point in second]
        ax1, ay1, ax2, ay2 = min(first_x), min(first_y), max(first_x), max(first_y)
        bx1, by1, bx2, by2 = min(second_x), min(second_y), max(second_x), max(second_y)
        intersection = max(0, min(ax2, bx2) - max(ax1, bx1)) * max(0, min(ay2, by2) - max(ay1, by1))
        first_area, second_area = max(1, (ax2 - ax1) * (ay2 - ay1)), max(1, (bx2 - bx1) * (by2 - by1))
        return intersection / min(first_area, second_area)


class PipelineService(QObject):
    progress = Signal(str, str, int, int, str)
    image_finished = Signal(str, object)
    image_failed = Signal(str, str)
    completed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._thread: QThread | None = None
        self._worker: PipelineWorker | None = None
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def process_project(self, project, image_ids: set[str] | None = None) -> bool:
        if self.running:
            return False
        items = [
            asdict(item) for item in project.images
            if item.status in {"queued", "failed", "cancelled"}
            and (image_ids is None or item.id in image_ids)
        ]
        if not items:
            return False
        self._cancel.clear()
        self._thread = QThread()
        config = {
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
            "qwen_model_path": SETTINGS.qwen_model_path,
            "qwen_model_name": SETTINGS.qwen_model_name,
        }
        self._worker = PipelineWorker(items, project.artifacts, project.target_language, self._cancel, config)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.stage.connect(self.progress)
        self._worker.image_finished.connect(self.image_finished)
        self._worker.image_failed.connect(self.image_failed)
        self._worker.finished.connect(self._finish)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup)
        self._thread.start()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    @Slot(bool)
    def _finish(self, cancelled: bool) -> None:
        self.completed.emit(cancelled)

    @Slot()
    def _cleanup(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None
