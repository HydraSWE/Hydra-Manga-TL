"""Sequential preprocessing and OCR helpers for PipelineWorker."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from hydra_manga_tl import __version__
from hydra_manga_tl.core.language import resolve_source_language
from hydra_manga_tl.phase.pipeline_helpers import (
    _cache_fragment,
    _ocr_cache_key,
    _ocr_settings_fingerprint,
    _preprocessing_settings_fingerprint,
    _preprocessing_stage_fingerprint,
    _record_stage_completion,
    _stable_json_hash,
    _state_manager_for_stages,
    _write_json_atomic,
)
from hydra_manga_tl.phase.preprocessor import prepare_ocr_image
from hydra_manga_tl.phase.state_manager import (
    StageAction,
    StageContract,
    StageValidationRequest,
)


class PipelineWorkerSequentialOcrMixin:
    def _run_sequential_ocr(
        self,
        *,
        image_id: str,
        source: Path,
        position: int,
        total: int,
        job_manifest,
        timing: dict[str, Any],
        requested_source: str,
        quality: str,
        chapter_ocr_language: str | None,
        ocr_service,
        paths,
        current_rss_mb,
    ) -> dict[str, Any]:
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
        _record_stage_completion(
            job_manifest,
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
            return {"cancelled": True}

        stage_started = time.perf_counter()
        ocr_cache_dir = paths.ocr_cache
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
        _record_stage_completion(
            job_manifest,
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
            return {"cancelled": True}
        return {
            "cancelled": False,
            "chapter_ocr_language": chapter_ocr_language,
            "source_language": source_language,
            "ocr_path": ocr_path,
            "preprocessed": preprocessed,
            "ocr_result": ocr_result,
            "source_regions": source_regions,
        }
