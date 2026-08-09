"""Fast pipeline execution for PipelineWorker."""

from __future__ import annotations

import gc
import json
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from hydra_manga_tl.phase.job_manifest import JobManifest
from hydra_manga_tl.phase.pipeline_helpers import (
    _cache_fragment,
    _initial_ocr_language,
    _ocr_engine_languages,
    _page_translation_cache_key,
    _translation_stage_fingerprint,
    _write_json_atomic,
)
from hydra_manga_tl.project.artifacts import target_manifest_path
from hydra_manga_tl.translation.queue import RequestCancelled
from hydra_manga_tl.translation.scheduler import (
    ParallelPageJob,
    PageTranslationOutcome,
    ProviderDispatcher,
    SchedulerSnapshot,
    SmartPageJob,
)


class PipelineWorkerFastRunMixin:
    def _run_fast(self) -> bool:
        paths = self.paths()
        ocr_service_cls = self.ocr_service_factory()
        translation_runtime = self.translation_runtime()
        scheduler_cls = self.scheduler_factory()
        logger = self.logger()

        cancelled = False
        batch_started = time.perf_counter()
        batch_timings: list[dict[str, Any]] = []
        ocr_service = None
        translation_executor: ThreadPoolExecutor | None = None
        try:
            self.artifacts.mkdir(parents=True, exist_ok=True)
            requested_source = self.config.get("source_language", "auto")
            preferred = _initial_ocr_language(
                requested_source, "Fast", self.config.get("auto_primary_language") or None,
            )
            ocr_service = ocr_service_cls(
                _ocr_engine_languages(requested_source, "Fast", preferred),
                use_subprocess=bool(self.config.get("ocr_subprocess_enabled", False)),
                recycle_pages=int(self.config.get("ocr_worker_recycle_pages", 25)),
                memory_limit_mb=int(self.config.get("ocr_worker_memory_limit_mb", 2048)),
                retry_stats_path=paths.cache / "ocr_retry_stats.json",
            )
            self.target_artifacts.mkdir(parents=True, exist_ok=True)
            job_manifest = JobManifest.load(
                target_manifest_path(self.artifacts, self.target)
            )
            session = translation_runtime.fast_session(
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
                cache_path = paths.page_translation_cache / (
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
                            logger.warning(
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
                        paths.page_translation_cache
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

            scheduler = scheduler_cls(
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
