"""Sequential translation assembly helpers for PipelineWorker."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from hydra_manga_tl.core.normalization import normalize_global_text
from hydra_manga_tl.core.region_types import normalize_region_type
from hydra_manga_tl.phase.pipeline_helpers import (
    _auto_translate_region_type,
    _cache_fragment,
    _page_translation_cache_key,
    _translation_stage_fingerprint,
    _write_json_atomic,
)
from hydra_manga_tl.translation.engines import PageTranslation


class PipelineWorkerSequentialTranslationMixin:
    def _translate_sequential_groups(
        self,
        *,
        source: Path,
        image_id: str,
        source_language: str,
        effective_groups: list,
        group_payloads: list[dict[str, Any]],
        group_source_polygons: list,
        source_regions: list[dict[str, Any]],
        context_engine,
        layout_graph: dict[str, Any],
        position: int,
        total: int,
        job_manifest,
        item: dict[str, Any],
        timing: dict[str, Any],
        translation_pool,
        stage_started: float,
        paths,
        translation_runtime,
    ) -> dict[str, Any] | None:
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
                layout_graph,
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
            return None
        if not dialogue:
            page_result = PageTranslation(source_language, self.target, [])
        else:
            translation_cache_dir = paths.page_translation_cache
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
                    page_result = translation_runtime.translate_cached_page(
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

        translated_map = {
            str(item.get("id")): normalize_global_text(str(item.get("text", "")))
            for item in page_result.translations
        }
        translated_details = {
            str(item.get("id")): dict(item)
            for item in page_result.translations
        }

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
                        else translation_runtime.last_engine_id
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

        return {
            "dialogue": dialogue,
            "page": page,
            "page_result": page_result,
            "page_cache_key": page_cache_key,
            "translation_input_fingerprint": translation_input_fingerprint,
            "translated_groups": translated_groups,
            "bubble_segmentations": bubble_segmentations,
        }
