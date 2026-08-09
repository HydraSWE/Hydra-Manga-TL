"""Bubble-level Fast translation scheduler."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import logging
import threading
import time
import sys
from typing import Callable

from hydra_manga_tl.translation.engines import PageTranslation
from hydra_manga_tl.translation.runtime import (
    _terminal_provider_error,
    _transient_provider_error,
)
from hydra_manga_tl.translation.service import TranslationHTTPError
from hydra_manga_tl.translation.scheduler_models import (
    BubbleTranslationUnit,
    DEFAULT_PROVIDER_PROFILES,
    PageTranslationOutcome,
    ProviderDispatcher,
    ProviderProfile,
    SchedulerSnapshot,
    SmartPageJob,
    TokenBudgetManager,
    TranslationQueueItem,
    TranslationResultStore,
    resolve_provider_worker_count,
)
from hydra_manga_tl.translation.scheduler_smart_cache import SmartSchedulerCacheMixin

LOGGER = logging.getLogger(__name__)
PROVIDER_FUTURE_POLL_SECONDS = 2.0


def _provider_future_poll_seconds() -> float:
    facade = sys.modules.get("hydra_manga_tl.translation.scheduler")
    return float(getattr(facade, "PROVIDER_FUTURE_POLL_SECONDS", PROVIDER_FUTURE_POLL_SECONDS))


class SmartTranslationScheduler(SmartSchedulerCacheMixin):
    """Bubble-level Fast scheduler with TM prefiltering and token batches."""

    def __init__(
        self,
        *,
        primary_provider: str,
        fallback_provider: str = "",
        profiles: dict[str, ProviderProfile] | None = None,
        glossary: dict[str, str] | None = None,
        worker_override: int = 0,
        translation_memory_enabled: bool = True,
        prefer_verified_tm: bool = True,
        cancel_event: threading.Event,
        gpu_state: str = "",
        snapshot_callback: Callable[[SchedulerSnapshot], None] | None = None,
    ) -> None:
        self.primary_provider = str(primary_provider or "qwen").strip().lower()
        fallback = str(fallback_provider or "").strip().lower()
        self.fallback_provider = fallback if fallback != self.primary_provider else ""
        self.profiles = {**DEFAULT_PROVIDER_PROFILES, **(profiles or {})}
        self.glossary = glossary or {}
        self.worker_override = max(0, min(6, int(worker_override or 0)))
        self.translation_memory_enabled = bool(translation_memory_enabled)
        self.prefer_verified_tm = bool(prefer_verified_tm)
        self.cancel_event = cancel_event
        self.gpu_state = gpu_state
        self.snapshot_callback = snapshot_callback
        self.store = TranslationResultStore()
        self.provider_calls = 0
        self.retries = 0
        self.cache_hits = 0
        self.failed = 0

    def run(
        self,
        jobs: list[SmartPageJob],
        dispatcher: ProviderDispatcher,
        commit: Callable[[PageTranslationOutcome], PageTranslationOutcome | None],
    ) -> list[PageTranslationOutcome]:
        ordered = sorted(jobs, key=lambda item: item.page_index)
        units_by_page = {job.page_index: self._units_for_job(job) for job in ordered}
        queued: list[TranslationQueueItem] = []
        total_units = sum(len(units) for units in units_by_page.values())
        for job in ordered:
            if self._hydrate_legacy_page_cache(job):
                self.cache_hits += len(job.page.dialogue)
                continue
            for unit in units_by_page[job.page_index]:
                if self._hydrate_cached_or_tm(job, unit):
                    self.cache_hits += 1
                    continue
                queued.append(TranslationQueueItem(
                    unit,
                    TokenBudgetManager.estimate_text_tokens(unit.source_text),
                ))
        completed_units = total_units - len(queued)
        outcomes_by_page: dict[int, PageTranslationOutcome] = {}
        committed_pages: set[int] = set()

        def page_outcome(job: SmartPageJob) -> PageTranslationOutcome:
            started = time.perf_counter()
            page_result = self.store.page_translation(job.page)
            missing = [
                str(item.get("id", ""))
                for item in page_result.translations
                if not str(item.get("text", "")).strip()
            ]
            return PageTranslationOutcome(
                page_index=job.page_index,
                image_id=job.image_id,
                request_id=job.request_id,
                translation=None if missing else page_result,
                provider_id="smart-scheduler",
                attempts=1,
                elapsed_seconds=time.perf_counter() - started,
                error=(
                    f"Smart scheduler did not translate: {', '.join(missing)}"
                    if missing else ""
                ),
                cancelled=self.cancel_event.is_set(),
            )

        def commit_ready_pages(
            current_completed_units: int,
            current_queued: int,
        ) -> None:
            for job in ordered:
                if job.page_index in committed_pages:
                    continue
                units = units_by_page.get(job.page_index, [])
                if any(not self.store.has(unit.bubble_id) for unit in units):
                    continue
                outcome = page_outcome(job)
                if outcome.succeeded and outcome.translation is not None:
                    self._write_legacy_page_cache(job, outcome.translation)
                committed = commit(outcome) or outcome
                outcomes_by_page[job.page_index] = committed
                committed_pages.add(job.page_index)
                self._publish(
                    queued=current_queued,
                    completed_units=current_completed_units,
                    total_units=total_units,
                    total_pages=len(ordered),
                    page_completed=len(committed_pages),
                )

        commit_ready_pages(completed_units, len(queued))
        self._publish(
            queued=len(queued),
            completed_units=completed_units,
            total_units=total_units,
            total_pages=len(ordered),
        )

        if queued and not self.cancel_event.is_set():
            completed_units = self._drain_provider_queue(
                queued,
                dispatcher,
                completed_units=completed_units,
                total_units=total_units,
                total_pages=len(ordered),
                progress_callback=commit_ready_pages,
            )

        for job in ordered:
            if job.page_index in committed_pages:
                continue
            outcome = page_outcome(job)
            if outcome.succeeded and outcome.translation is not None:
                self._write_legacy_page_cache(job, outcome.translation)
            committed = commit(outcome) or outcome
            outcomes_by_page[job.page_index] = committed
            committed_pages.add(job.page_index)
            self._publish(
                queued=0,
                completed_units=completed_units,
                total_units=total_units,
                total_pages=len(ordered),
                page_completed=len(committed_pages),
            )
        return [
            outcomes_by_page[job.page_index]
            for job in ordered
            if job.page_index in outcomes_by_page
        ]

    def _drain_provider_queue(
        self,
        queued: list[TranslationQueueItem],
        dispatcher: ProviderDispatcher,
        *,
        completed_units: int,
        total_units: int,
        total_pages: int,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> int:
        provider = self.primary_provider
        unhealthy: set[str] = set()
        managers: dict[str, TokenBudgetManager] = {}
        active: dict[
            Future[tuple[PageTranslation, str]],
            tuple[str, list[TranslationQueueItem], float],
        ] = {}

        executor = ThreadPoolExecutor(
            max_workers=6,
            thread_name_prefix="HydraProvider",
        )
        try:
            while (queued or active) and not self.cancel_event.is_set():
                if provider in unhealthy:
                    provider = self.fallback_provider
                if not provider and not active:
                    self.failed += len(queued)
                    queued.clear()
                    break
                effective_workers = max(1, len(active))
                if provider:
                    profile = self.profiles.get(provider, self.profiles["marian"])
                    manager = managers.setdefault(provider, TokenBudgetManager(profile))
                    effective_workers = resolve_provider_worker_count(
                        profile,
                        self.worker_override,
                        len(queued) + len(active),
                    )
                    while (
                        queued
                        and not self.cancel_event.is_set()
                        and len(active) < effective_workers
                    ):
                        batch = manager.next_batch(queued)
                        for item in batch:
                            queued.remove(item)
                        future = executor.submit(
                            dispatcher.dispatch,
                            provider,
                            [item.unit for item in batch],
                        )
                        active[future] = (provider, batch, time.monotonic())
                        self._publish(
                            queued=len(queued),
                            completed_units=completed_units,
                            total_units=total_units,
                            total_pages=total_pages,
                            active_provider=provider,
                            active_workers=len(active),
                            configured_workers=effective_workers,
                            current_token_budget=manager.current_target,
                            active_batches=len(active),
                            in_flight_units=sum(len(batch) for _, batch, _ in active.values()),
                            provider_status="sending",
                        )

                if not active:
                    continue
                poll_timeout = _provider_future_poll_seconds()
                now = time.monotonic()
                for batch_provider, _batch, started in active.values():
                    profile = self.profiles.get(
                        batch_provider,
                        self.profiles["marian"],
                    )
                    remaining = (
                        started
                        + max(0.01, float(profile.request_timeout))
                        - now
                    )
                    poll_timeout = min(poll_timeout, max(0.01, remaining))
                done, _ = wait(
                    tuple(active),
                    timeout=poll_timeout,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    active_provider = next(
                        (batch_provider for batch_provider, _batch, _started in active.values()),
                        provider,
                    )
                    active_budget = 0
                    if active_provider:
                        active_profile = self.profiles.get(active_provider, self.profiles["marian"])
                        active_budget = managers.setdefault(
                            active_provider,
                            TokenBudgetManager(active_profile),
                        ).current_target
                    self._publish(
                        queued=len(queued),
                        completed_units=completed_units,
                        total_units=total_units,
                        total_pages=total_pages,
                        active_provider=active_provider,
                        active_workers=len(active),
                        configured_workers=effective_workers,
                        current_token_budget=active_budget,
                        active_batches=len(active),
                        in_flight_units=sum(len(batch) for _, batch, _ in active.values()),
                        provider_status="waiting",
                    )
                    now = time.monotonic()
                    timed_out: list[Future[tuple[PageTranslation, str]]] = []
                    for future, (batch_provider, batch, started) in list(active.items()):
                        profile = self.profiles.get(
                            batch_provider,
                            self.profiles["marian"],
                        )
                        if now - started < max(0.01, float(profile.request_timeout)):
                            continue
                        timed_out.append(future)
                        active.pop(future, None)
                        future.cancel()
                        self.retries += 1
                        retryable = max(item.attempts for item in batch) < 2
                        if retryable:
                            manager = managers.setdefault(
                                batch_provider,
                                TokenBudgetManager(profile),
                            )
                            manager.reduce_after_rate_limit()
                            queued[:0] = [
                                TranslationQueueItem(
                                    item.unit,
                                    item.estimated_tokens,
                                    item.attempts + 1,
                                )
                                for item in batch
                            ]
                        elif self.fallback_provider and batch_provider != self.fallback_provider:
                            unhealthy.add(batch_provider)
                            provider = self.fallback_provider
                            queued[:0] = batch
                        else:
                            self.failed += len(batch)
                        LOGGER.warning(
                            "Fast provider batch timed out provider=%s units=%d "
                            "elapsed=%.1fs retryable=%s fallback=%s",
                            batch_provider,
                            len(batch),
                            now - started,
                            retryable,
                            self.fallback_provider or "",
                        )
                    self._publish(
                        queued=len(queued),
                        completed_units=completed_units,
                        total_units=total_units,
                        total_pages=total_pages,
                        active_provider=provider,
                        active_workers=len(active),
                        configured_workers=effective_workers,
                        active_batches=len(active),
                        in_flight_units=sum(len(batch) for _, batch, _ in active.values()),
                        provider_status="retrying" if timed_out else "waiting",
                    )
                    if timed_out:
                        continue
                    continue
                for future in done:
                    batch_provider, batch, _started = active.pop(future)
                    profile = self.profiles.get(
                        batch_provider,
                        self.profiles["marian"],
                    )
                    manager = managers.setdefault(
                        batch_provider,
                        TokenBudgetManager(profile),
                    )
                    try:
                        result, provider_id = future.result()
                    except Exception as error:
                        retry_after = (
                            error.retry_after
                            if isinstance(error, TranslationHTTPError)
                            and error.retry_after is not None
                            else profile.cooldown
                        )
                        if (
                            isinstance(error, TranslationHTTPError)
                            and error.status == 429
                        ):
                            manager.reduce_after_rate_limit()
                            self.retries += 1
                            retryable = max(item.attempts for item in batch) < 2
                            if retryable:
                                queued[:0] = [
                                    TranslationQueueItem(
                                        item.unit,
                                        item.estimated_tokens,
                                        item.attempts + 1,
                                    )
                                    for item in batch
                                ]
                                self._publish(
                                    queued=len(queued),
                                    completed_units=completed_units,
                                    total_units=total_units,
                                    total_pages=total_pages,
                                    active_provider=batch_provider,
                                    active_workers=len(active),
                                    configured_workers=effective_workers,
                                    current_token_budget=manager.current_target,
                                    active_batches=len(active),
                                    in_flight_units=sum(len(active_batch) for _, active_batch, _ in active.values()),
                                    provider_status="retrying",
                                )
                                self._wait_retry(retry_after)
                                continue
                            if (
                                self.fallback_provider
                                and batch_provider != self.fallback_provider
                            ):
                                unhealthy.add(batch_provider)
                                provider = self.fallback_provider
                                queued[:0] = batch
                                continue
                            self.failed += len(batch)
                            continue
                        if _terminal_provider_error(error):
                            unhealthy.add(batch_provider)
                            provider = self.fallback_provider
                            if provider:
                                queued[:0] = batch
                                continue
                        if (
                            _transient_provider_error(error)
                            and max(item.attempts for item in batch) < 2
                        ):
                            self.retries += 1
                            manager.reduce_after_rate_limit()
                            queued[:0] = [
                                TranslationQueueItem(
                                    item.unit,
                                    item.estimated_tokens,
                                    item.attempts + 1,
                                )
                                for item in batch
                            ]
                            self._publish(
                                queued=len(queued),
                                completed_units=completed_units,
                                total_units=total_units,
                                total_pages=total_pages,
                                active_provider=batch_provider,
                                active_workers=len(active),
                                configured_workers=effective_workers,
                                current_token_budget=manager.current_target,
                                active_batches=len(active),
                                in_flight_units=sum(len(active_batch) for _, active_batch, _ in active.values()),
                                provider_status="retrying",
                            )
                            self._wait_retry(retry_after)
                            continue
                        if (
                            _transient_provider_error(error)
                            and self.fallback_provider
                            and batch_provider != self.fallback_provider
                        ):
                            unhealthy.add(batch_provider)
                            provider = self.fallback_provider
                            queued[:0] = batch
                            continue
                        self.failed += len(batch)
                        continue
                    self.provider_calls += 1
                    self._store_dispatch_result(batch, result, provider_id)
                    for item in batch:
                        self._write_bubble_cache(item.unit, provider_id)
                    completed_units += len(batch)
                    manager.increase_after_success()
                    if progress_callback is not None:
                        progress_callback(completed_units, len(queued))
                    self._publish(
                        queued=len(queued),
                        completed_units=completed_units,
                        total_units=total_units,
                        total_pages=total_pages,
                        active_provider=batch_provider if active else provider,
                        active_workers=len(active),
                        configured_workers=effective_workers,
                        current_token_budget=manager.current_target,
                        active_batches=len(active),
                        in_flight_units=sum(len(batch) for _, batch, _ in active.values()),
                        provider_status="receiving" if active else "",
                    )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return completed_units

    def _wait_retry(self, delay: float) -> None:
        delay = max(0.0, min(30.0, float(delay or 0.0)))
        if delay:
            self.cancel_event.wait(delay)

    def _publish(
        self,
        *,
        queued: int,
        completed_units: int,
        total_units: int,
        total_pages: int,
        page_completed: int = 0,
        active_provider: str = "",
        active_workers: int | None = None,
        configured_workers: int | None = None,
        current_token_budget: int = 0,
        active_batches: int = 0,
        in_flight_units: int = 0,
        provider_status: str = "",
    ) -> None:
        if self.snapshot_callback is None:
            return
        profile = self.profiles.get(
            active_provider or self.primary_provider,
            self.profiles["marian"],
        )
        configured = (
            int(configured_workers)
            if configured_workers is not None
            else resolve_provider_worker_count(
                profile,
                self.worker_override,
                queued if queued else None,
            )
        )
        self.snapshot_callback(SchedulerSnapshot(
            configured_workers=max(1, configured),
            active_workers=max(
                0,
                int(active_workers)
                if active_workers is not None
                else (1 if active_provider else 0),
            ),
            queued=max(0, int(queued)),
            completed=max(0, int(page_completed)),
            failed=self.failed,
            total=max(0, int(total_pages)),
            gpu_state=self.gpu_state,
            ocr_total=total_pages,
            ocr_done=total_pages,
            translation_total=total_units,
            translation_done=completed_units,
            translation_cache_hits=self.cache_hits,
            provider_calls=self.provider_calls,
            retries=self.retries,
            render_total=total_pages,
            render_done=page_completed,
            active_provider=active_provider,
            current_token_budget=current_token_budget,
            active_batches=max(0, int(active_batches)),
            in_flight_units=max(0, int(in_flight_units)),
            provider_status=provider_status,
        ))


