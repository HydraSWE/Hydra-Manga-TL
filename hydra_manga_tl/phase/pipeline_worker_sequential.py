"""Sequential pipeline execution for PipelineWorker."""

from __future__ import annotations

import gc
import json
import time
import traceback
from importlib import import_module
from pathlib import Path

from PySide6.QtCore import Slot

from hydra_manga_tl import __version__
from hydra_manga_tl.phase.context_engine import ContextEngine
from hydra_manga_tl.phase.job_manifest import JobManifest
from hydra_manga_tl.phase.pipeline_helpers import (
    _initial_ocr_language,
    _model_identity,
    _ocr_engine_languages,
    _provider_identity,
    _record_stage_completion,
    _render_stage_fingerprint,
    _translation_settings_fingerprint,
    _write_json_atomic,
)
from hydra_manga_tl.phase.stage_streaming import BoundedStageExecutor
from hydra_manga_tl.project.artifacts import (
    target_manifest_path,
    target_translation_path,
)
from hydra_manga_tl.translation.queue import RequestCancelled


class PipelineWorkerSequentialMixin:
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
            ocr_service = self.ocr_service_factory()(
                _ocr_engine_languages(requested_source, quality, preferred),
                use_subprocess=bool(self.config.get("ocr_subprocess_enabled", False)),
                recycle_pages=int(self.config.get("ocr_worker_recycle_pages", 25)),
                memory_limit_mb=int(self.config.get("ocr_worker_memory_limit_mb", 2048)),
                retry_stats_path=self.paths().cache / "ocr_retry_stats.json",
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
                    ocr_state = self._run_sequential_ocr(
                        image_id=image_id,
                        source=source,
                        position=position,
                        total=total,
                        job_manifest=job_manifest,
                        timing=timing,
                        requested_source=requested_source,
                        quality=quality,
                        chapter_ocr_language=chapter_ocr_language,
                        ocr_service=ocr_service,
                        paths=self.paths(),
                        current_rss_mb=current_rss_mb,
                    )
                    if ocr_state.get("cancelled"):
                        cancelled = True
                        break
                    chapter_ocr_language = ocr_state["chapter_ocr_language"]
                    source_language = ocr_state["source_language"]
                    ocr_path = ocr_state["ocr_path"]
                    preprocessed = ocr_state["preprocessed"]
                    ocr_result = ocr_state["ocr_result"]
                    source_regions = ocr_state["source_regions"]
                    job_manifest.mark(image_id, "translating", stage="OCR")
                    self.stage.emit(image_id, "translating", position, total, f"Translating {source.name}")
                    stage_started = time.perf_counter()
                    group_state = self._build_sequential_groups(
                        source=source,
                        image_id=image_id,
                        source_regions=source_regions,
                        ocr_result=ocr_result,
                        timing=timing,
                    )
                    source_image = group_state["source_image"]
                    page_size = group_state["page_size"]
                    layout_graph = group_state["layout_graph"]
                    groups = group_state["groups"]
                    effective_groups = group_state["effective_groups"]
                    group_payloads = group_state["group_payloads"]
                    group_source_polygons = group_state["group_source_polygons"]
                    debug_artifacts = group_state["debug_artifacts"]
                    translation_state = self._translate_sequential_groups(
                        source=source,
                        image_id=image_id,
                        source_language=source_language,
                        effective_groups=effective_groups,
                        group_payloads=group_payloads,
                        group_source_polygons=group_source_polygons,
                        source_regions=source_regions,
                        context_engine=context_engine,
                        layout_graph=layout_graph.to_dict(),
                        position=position,
                        total=total,
                        job_manifest=job_manifest,
                        item=item,
                        timing=timing,
                        translation_pool=translation_pool,
                        stage_started=stage_started,
                        paths=self.paths(),
                        translation_runtime=self.translation_runtime(),
                    )
                    if translation_state is None:
                        continue
                    dialogue = translation_state["dialogue"]
                    page = translation_state["page"]
                    page_result = translation_state["page_result"]
                    page_cache_key = translation_state["page_cache_key"]
                    translation_input_fingerprint = translation_state[
                        "translation_input_fingerprint"
                    ]
                    translated_groups = translation_state["translated_groups"]
                    bubble_segmentations = translation_state["bubble_segmentations"]
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
                                self.translation_runtime().last_engine_id
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

                    self._finalize_sequential_render(
                        image_id=image_id,
                        source=source,
                        source_language=source_language,
                        position=position,
                        total=total,
                        item=item,
                        translation_path=translation_path,
                        translated_groups=translated_groups,
                        page_size=page_size,
                        dialogue=dialogue,
                        page=page,
                        page_result=page_result,
                        page_cache_key=page_cache_key,
                        phase2=phase2,
                        translation_input_fingerprint=translation_input_fingerprint,
                        ocr_path=ocr_path,
                        render_input_fingerprint=render_input_fingerprint,
                        timing=timing,
                        image_started=image_started,
                        current_rss_mb=current_rss_mb,
                        ocr_service=ocr_service,
                        preprocessed=preprocessed,
                        ocr_result=ocr_result,
                        layout_graph=layout_graph,
                        bubble_segmentations=bubble_segmentations,
                        debug_artifacts=debug_artifacts,
                        job_manifest=job_manifest,
                        context_engine=context_engine,
                        batch_timings=batch_timings,
                        translation_runtime=self.translation_runtime(),
                    )
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
