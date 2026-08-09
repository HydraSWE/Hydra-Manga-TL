"""Project planning helpers for PipelineService."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from hydra_manga_tl import __version__
from hydra_manga_tl.phase.job_manifest import JobManifest
from hydra_manga_tl.phase.pipeline_helpers import (
    _clear_page_retranslate_artifacts,
    _completed_project_output,
    _existing_artifacts,
    _model_identity,
    _provider_identity,
    _render_settings_fingerprint,
    _render_stage_fingerprint,
    _state_manager_for_stages,
    _translation_settings_fingerprint,
)
from hydra_manga_tl.phase.state_manager import (
    StageContract,
    StageValidationRequest,
)
from hydra_manga_tl.project.artifacts import (
    rendered_filename,
    target_manifest_path,
    target_render_dir,
    target_translation_path,
)
from hydra_manga_tl.translation.requests import TranslationRequestStatus


class PipelineServicePlanningMixin:
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
                _clear_page_retranslate_artifacts(
                    project,
                    image,
                    paths=self.paths(),
                )
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
