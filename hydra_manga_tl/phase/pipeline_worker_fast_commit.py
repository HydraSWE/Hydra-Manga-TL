"""Fast pipeline outcome commit behavior for PipelineWorker."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any

from hydra_manga_tl import __version__
from hydra_manga_tl.core.normalization import normalize_global_text
from hydra_manga_tl.ocr.core import OCRResult
from hydra_manga_tl.ocr.service import current_rss_mb
from hydra_manga_tl.phase.intelligent_page import IntelligentPageResult
from hydra_manga_tl.phase.layout import TextGroup
from hydra_manga_tl.phase.pipeline_helpers import (
    _auto_translate_region_type,
    _existing_artifacts,
    _model_identity,
    _provider_identity,
    _record_stage_completion,
    _render_settings_fingerprint,
    _render_stage_fingerprint,
    _stable_json_hash,
    _translation_settings_fingerprint,
    _write_json_atomic,
)
from hydra_manga_tl.phase.review import review_translation_groups
from hydra_manga_tl.phase.typesetting import review_rendered_group, summarize_render_review
from hydra_manga_tl.project.artifacts import rendered_filename, target_render_dir, target_translation_path
from hydra_manga_tl.translation.requests import RenderRequest
from hydra_manga_tl.translation.scheduler import PageTranslationOutcome


class PipelineWorkerFastCommitMixin:
    """Commit fast-path translations into rendered artifacts and manifests."""
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


