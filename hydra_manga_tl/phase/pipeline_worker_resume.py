"""Verified translation/render resume behavior for PipelineWorker."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from PIL import Image

from hydra_manga_tl import __version__
from hydra_manga_tl.phase.intelligent_page import IntelligentPageResult
from hydra_manga_tl.phase.job_manifest import JobManifest
from hydra_manga_tl.phase.pipeline_helpers import (
    _existing_artifacts,
    _model_identity,
    _provider_identity,
    _record_stage_completion,
    _render_settings_fingerprint,
    _render_stage_fingerprint,
    _stable_json_hash,
    _state_manager_for_stages,
    _translation_settings_fingerprint,
    _write_json_atomic,
)
from hydra_manga_tl.phase.review import review_translation_groups
from hydra_manga_tl.phase.state_manager import StageAction, StageContract, StageValidationRequest
from hydra_manga_tl.phase.typesetting import review_rendered_group, summarize_render_review
from hydra_manga_tl.project.artifacts import rendered_filename, target_render_dir, target_translation_path
from hydra_manga_tl.translation.requests import RenderRequest


class PipelineWorkerResumeMixin:
    """Reuse verified translation/render artifacts when stage metadata matches."""
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


