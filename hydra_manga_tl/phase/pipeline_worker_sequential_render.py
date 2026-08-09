"""Sequential render and review finalization helpers for PipelineWorker."""

from __future__ import annotations

import json
import time

from hydra_manga_tl import __version__
from hydra_manga_tl.phase.intelligent_page import IntelligentPageResult
from hydra_manga_tl.phase.pipeline_helpers import (
    _existing_artifacts,
    _model_identity,
    _provider_identity,
    _record_stage_completion,
    _render_settings_fingerprint,
    _stable_json_hash,
    _translation_settings_fingerprint,
    _write_json_atomic,
)
from hydra_manga_tl.phase.review import review_translation_groups
from hydra_manga_tl.phase.typesetting import review_rendered_group, summarize_render_review
from hydra_manga_tl.project.artifacts import rendered_filename, target_render_dir
from hydra_manga_tl.translation.requests import RenderRequest


class PipelineWorkerSequentialRenderMixin:
    def _finalize_sequential_render(
        self,
        *,
        image_id,
        source,
        source_language,
        position,
        total,
        item,
        translation_path,
        translated_groups,
        page_size,
        dialogue,
        page,
        page_result,
        page_cache_key,
        phase2,
        translation_input_fingerprint,
        ocr_path,
        render_input_fingerprint,
        timing,
        image_started,
        current_rss_mb,
        ocr_service,
        preprocessed,
        ocr_result,
        layout_graph,
        bubble_segmentations,
        debug_artifacts,
        job_manifest,
        context_engine,
        batch_timings,
        translation_runtime,
    ) -> None:
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
                    translation_runtime.last_engine_id
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
