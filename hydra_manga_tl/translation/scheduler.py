"""Bounded, ordered page translation scheduling for Fast mode."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import logging
from typing import Any, Callable, Iterable, Protocol

from hydra_manga_tl.core.normalization import normalize_global_text
from hydra_manga_tl.core.region_types import normalize_region_type
from hydra_manga_tl.translation.memory import TRANSLATION_MEMORY
from hydra_manga_tl.translation.engines import PageDialogue, PageTranslation
from hydra_manga_tl.translation.runtime import (
    _terminal_provider_error,
    _transient_provider_error,
)
from hydra_manga_tl.translation.service import TranslationHTTPError


LOGGER = logging.getLogger(__name__)
PROVIDER_FUTURE_POLL_SECONDS = 2.0


@dataclass(frozen=True)
class ParallelPageJob:
    page_index: int
    image_id: str
    request_id: str
    prepared_path: Path
    cache_path: Path
    page: PageDialogue | None = None


@dataclass(frozen=True)
class PageTranslationOutcome:
    page_index: int
    image_id: str
    request_id: str
    translation: PageTranslation | None = None
    provider_id: str = ""
    attempts: int = 0
    elapsed_seconds: float = 0.0
    error: str = ""
    cancelled: bool = False

    @property
    def succeeded(self) -> bool:
        return self.translation is not None and not self.error and not self.cancelled


@dataclass(frozen=True)
class SchedulerSnapshot:
    configured_workers: int
    active_workers: int
    queued: int
    completed: int
    failed: int
    total: int
    gpu_state: str
    ocr_total: int = 0
    ocr_done: int = 0
    translation_total: int = 0
    translation_done: int = 0
    translation_cache_hits: int = 0
    provider_calls: int = 0
    retries: int = 0
    render_total: int = 0
    render_done: int = 0
    active_provider: str = ""
    current_token_budget: int = 0
    active_batches: int = 0
    in_flight_units: int = 0
    provider_status: str = ""


@dataclass(frozen=True)
class ProviderProfile:
    key: str
    label: str
    default_parallel: int = 1
    max_parallel: int = 1
    target_tokens: int = 1800
    max_tokens: int = 2500
    rpm: int = 0
    tpm: int = 0
    cooldown: float = 0.0
    request_timeout: float = 90.0


DEFAULT_PROVIDER_PROFILES: dict[str, ProviderProfile] = {
    "groq": ProviderProfile(
        "groq", "Groq", default_parallel=1, max_parallel=2, target_tokens=1800,
        max_tokens=2500, rpm=30, tpm=6000, cooldown=2.0,
    ),
    "gemini": ProviderProfile(
        "gemini", "Gemini", default_parallel=2, max_parallel=4, target_tokens=3500,
        max_tokens=6000,
    ),
    "openai": ProviderProfile(
        "openai", "OpenAI", default_parallel=2, max_parallel=4, target_tokens=4000,
        max_tokens=8000,
    ),
    "openai_compatible": ProviderProfile(
        "openai_compatible", "OpenAI-Compatible", default_parallel=1, max_parallel=2,
        target_tokens=3000, max_tokens=6000, cooldown=1.0, request_timeout=120.0,
    ),
    "qwen": ProviderProfile(
        "qwen", "Local Qwen", default_parallel=1, max_parallel=1, target_tokens=4000,
        max_tokens=8000,
    ),
    "marian": ProviderProfile(
        "marian", "MarianMT", default_parallel=1, max_parallel=1, target_tokens=1200,
        max_tokens=2000,
    ),
    "google": ProviderProfile(
        "google", "Google Translate", default_parallel=2, max_parallel=4, target_tokens=2000,
        max_tokens=4000,
    ),
    "deepseek": ProviderProfile(
        "deepseek", "DeepSeek", default_parallel=2, max_parallel=3, target_tokens=3000,
        max_tokens=6000,
    ),
}


@dataclass(frozen=True)
class BubbleTranslationUnit:
    bubble_id: str
    legacy_id: str
    page_index: int
    page_number: int
    image_id: str
    request_id: str
    source_language: str
    target_language: str
    source_text: str
    region_type: str
    page_context: str = ""
    bubble_cache_dir: Path | None = None
    display_id: str = ""
    bbox: list[list[int]] | None = None
    source_text_hash: str = ""
    source_region_hash: str | None = None


@dataclass(frozen=True)
class TranslationQueueItem:
    unit: BubbleTranslationUnit
    estimated_tokens: int
    attempts: int = 0


@dataclass(frozen=True)
class SmartPageJob:
    page_index: int
    image_id: str
    request_id: str
    prepared_path: Path
    cache_path: Path
    bubble_cache_dir: Path
    page: PageDialogue


class TranslationResultStore:
    """Store translated bubbles by durable id and reconstruct page results."""

    def __init__(self) -> None:
        self._results: dict[str, dict[str, Any]] = {}

    def set(
        self,
        unit: BubbleTranslationUnit,
        text: str,
        *,
        provider_id: str,
        translation_source: str = "provider",
        tm_match_type: str = "",
        tm_entry_id: int | None = None,
    ) -> None:
        self._results[unit.bubble_id] = {
            "id": unit.legacy_id,
            "bubble_id": unit.bubble_id,
            "text": normalize_global_text(str(text)),
            "provider_id": provider_id,
            "translation_source": translation_source,
            "tm_match_type": tm_match_type,
            "tm_entry_id": tm_entry_id,
        }

    def has(self, bubble_id: str) -> bool:
        return bubble_id in self._results

    def page_translation(self, page: PageDialogue) -> PageTranslation:
        translations: list[dict[str, Any]] = []
        for item in page.dialogue:
            bubble_id = str(item.get("bubble_id") or item.get("id", ""))
            legacy_id = str(item.get("id", ""))
            value = dict(self._results.get(bubble_id, {}))
            if not value:
                value = {
                    "id": legacy_id,
                    "bubble_id": bubble_id,
                    "text": "",
                    "provider_id": "",
                    "translation_source": "",
                    "tm_match_type": "",
                    "tm_entry_id": None,
                }
            value["id"] = legacy_id
            translations.append(value)
        return PageTranslation(
            page.source_language,
            page.target_language,
            translations,
        )


class TokenBudgetManager:
    def __init__(self, profile: ProviderProfile) -> None:
        self.profile = profile
        self.current_target = max(1, int(profile.target_tokens))
        self._successes = 0

    @staticmethod
    def estimate_text_tokens(value: str) -> int:
        text = str(value or "").strip()
        if not text:
            return 1
        cjk = sum(
            1 for char in text
            if "\u3040" <= char <= "\u30ff" or "\u3400" <= char <= "\u9fff"
        )
        latinish = max(0, len(text) - cjk)
        return max(1, cjk + (latinish + 3) // 4 + 12)

    def next_batch(self, queued: list[TranslationQueueItem]) -> list[TranslationQueueItem]:
        if not queued:
            return []
        batch: list[TranslationQueueItem] = []
        total = 0
        target = min(self.current_target, self.profile.max_tokens)
        for item in queued:
            estimate = max(1, int(item.estimated_tokens))
            if batch and total + estimate > target:
                break
            if not batch and estimate > self.profile.max_tokens:
                batch.append(item)
                break
            if total + estimate > self.profile.max_tokens:
                break
            batch.append(item)
            total += estimate
        return batch or [queued[0]]

    def reduce_after_rate_limit(self) -> None:
        self.current_target = max(1, int(self.current_target * 0.8))
        self._successes = 0

    def increase_after_success(self) -> None:
        self._successes += 1
        if self._successes < 5:
            return
        self._successes = 0
        self.current_target = min(
            self.profile.target_tokens,
            max(self.current_target + 1, int(self.current_target * 1.1)),
        )


class ProviderDispatcher:
    """Thin provider call wrapper; scheduling decisions live above it."""

    def __init__(self, session) -> None:
        self.session = session

    def dispatch(
        self,
        provider_key: str,
        units: list[BubbleTranslationUnit],
    ) -> tuple[PageTranslation, str]:
        if not units:
            return PageTranslation("", "", []), provider_key
        page = PageDialogue(
            units[0].source_language,
            units[0].target_language,
            [
                {
                    "id": unit.bubble_id,
                    "text": unit.source_text,
                    "source_text": unit.source_text,
                    "region_type": unit.region_type,
                    "bbox": unit.bbox or [],
                    "source_text_hash": unit.source_text_hash,
                    "source_region_hash": unit.source_region_hash,
                    "reading_order": index + 1,
                }
                for index, unit in enumerate(units)
            ],
            units[0].page_context,
        )
        return self.session.manager.translate_page_using(provider_key, page)


def resolve_provider_worker_count(
    profile: ProviderProfile,
    override: int,
    queued_count: int | None = None,
) -> int:
    requested = (
        int(profile.default_parallel)
        if int(override or 0) == 0
        else max(1, min(6, int(override)))
    )
    resolved = max(1, min(requested, max(1, int(profile.max_parallel))))
    if queued_count is not None:
        resolved = min(resolved, max(1, int(queued_count)))
    return resolved


class SmartTranslationScheduler:
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
                poll_timeout = PROVIDER_FUTURE_POLL_SECONDS
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

    def _store_dispatch_result(
        self,
        batch: list[TranslationQueueItem],
        result: PageTranslation,
        provider_id: str,
    ) -> None:
        by_id = {
            str(item.get("id", "")): str(item.get("text", ""))
            for item in result.translations
        }
        for item in batch:
            unit = item.unit
            self.store.set(
                unit,
                by_id.get(unit.bubble_id, ""),
                provider_id=provider_id,
                translation_source="provider",
            )

    def _hydrate_cached_or_tm(
        self,
        job: SmartPageJob,
        unit: BubbleTranslationUnit,
    ) -> bool:
        cached = self._read_bubble_cache(job, unit)
        if cached is not None:
            self.store.set(
                unit,
                str(cached.get("text", "")),
                provider_id=str(cached.get("provider_id", "bubble-cache")),
                translation_source=str(cached.get("translation_source", "bubble-cache")),
                tm_match_type=str(cached.get("tm_match_type", "")),
                tm_entry_id=cached.get("tm_entry_id"),
            )
            return True
        match = (
            TRANSLATION_MEMORY.lookup(
                source_text=unit.source_text,
                source_language=unit.source_language,
                target_language=unit.target_language,
                region_type=unit.region_type,
                prefer_verified=self.prefer_verified_tm,
                engine_id=self.primary_provider,
                glossary=self.glossary,
                record_usage=False,
            )
            if self.translation_memory_enabled
            else None
        )
        if match is None:
            return False
        self.store.set(
            unit,
            match.translated_text,
            provider_id=match.entry.translation_provider,
            translation_source=match.source,
            tm_match_type=match.match_type,
            tm_entry_id=match.entry.id,
        )
        return True

    def _hydrate_legacy_page_cache(self, job: SmartPageJob) -> bool:
        try:
            payload = json.loads(job.cache_path.read_text(encoding="utf-8"))
            cached = PageTranslation(
                source_language=str(payload.get("source_language", job.page.source_language)),
                target_language=str(payload.get("target_language", job.page.target_language)),
                translations=list(payload.get("translations", [])),
            )
        except (OSError, TypeError, ValueError):
            return False
        by_id = {
            str(item.get("id", "")): dict(item)
            for item in cached.translations
        }
        if not by_id:
            return False
        units = self._units_for_job(job)
        for unit in units:
            cached_item = by_id.get(unit.legacy_id)
            if not cached_item or not str(cached_item.get("text", "")).strip():
                return False
        for unit in units:
            cached_item = by_id[unit.legacy_id]
            self.store.set(
                unit,
                str(cached_item.get("text", "")),
                provider_id=str(cached_item.get("provider_id", "page-cache")),
                translation_source=str(cached_item.get("translation_source", "page-cache")),
                tm_match_type=str(cached_item.get("tm_match_type", "")),
                tm_entry_id=cached_item.get("tm_entry_id"),
            )
        return True

    def _units_for_job(self, job: SmartPageJob) -> list[BubbleTranslationUnit]:
        units: list[BubbleTranslationUnit] = []
        for index, item in enumerate(job.page.dialogue):
            legacy_id = str(item.get("id", f"r{index + 1}"))
            bubble_id = str(item.get("bubble_id") or legacy_id)
            units.append(BubbleTranslationUnit(
                bubble_id=bubble_id,
                legacy_id=legacy_id,
                page_index=job.page_index,
                page_number=job.page_index + 1,
                image_id=job.image_id,
                request_id=job.request_id,
                source_language=job.page.source_language,
                target_language=job.page.target_language,
                source_text=str(item.get("text", "")),
                region_type=normalize_region_type(item.get("region_type") or item.get("type")),
                page_context=str(job.page.page_context or ""),
                bubble_cache_dir=job.bubble_cache_dir,
                display_id=str(item.get("display_id", "")),
                bbox=list(item.get("bbox", [])),
                source_text_hash=str(item.get("source_text_hash", "")),
                source_region_hash=item.get("source_region_hash"),
            ))
        return units

    def _bubble_cache_path(self, unit: BubbleTranslationUnit) -> Path:
        payload = {
            "kind": "bubble-translation-v1",
            "bubble_id": unit.bubble_id,
            "source_language": unit.source_language,
            "target_language": unit.target_language,
            "source_text": unit.source_text,
            "region_type": unit.region_type,
            "page_context": unit.page_context,
            "provider": self.primary_provider,
            "glossary": self.glossary,
        }
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        cache_dir = Path(unit.bubble_cache_dir or ".")
        return cache_dir / f"{unit.legacy_id}_{digest[:16]}.json"

    def _read_bubble_cache(
        self,
        job: SmartPageJob,
        unit: BubbleTranslationUnit,
    ) -> dict[str, Any] | None:
        path = self._bubble_cache_path(unit)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return None
        text = str(payload.get("text", "")).strip()
        return payload if text else None

    def _write_bubble_cache(
        self,
        unit: BubbleTranslationUnit,
        provider_id: str,
    ) -> None:
        value = self.store._results.get(unit.bubble_id)
        if not value:
            return
        path = self._bubble_cache_path(unit)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({
                "kind": "bubble-translation-v1",
                "bubble_id": unit.bubble_id,
                "legacy_id": unit.legacy_id,
                "display_id": unit.display_id,
                "source_text": unit.source_text,
                "text": value.get("text", ""),
                "provider_id": provider_id,
                "translation_source": value.get("translation_source", "provider"),
                "tm_match_type": value.get("tm_match_type", ""),
                "tm_entry_id": value.get("tm_entry_id"),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _write_legacy_page_cache(job: SmartPageJob, translation: PageTranslation) -> None:
        job.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = job.cache_path.with_suffix(job.cache_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(asdict(translation), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(job.cache_path)

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


class PageTranslationStage(Protocol):
    """Provider-neutral hook for future page translation stages."""

    def __call__(self, job: ParallelPageJob) -> PageTranslationOutcome: ...


def auto_worker_count(logical_threads: int | None = None) -> int:
    threads = max(1, int(logical_threads or os.cpu_count() or 1))
    if threads <= 4:
        return 2
    if threads <= 8:
        return 4
    return 6


def resolve_worker_count(override: int, page_count: int) -> int:
    requested = auto_worker_count() if int(override or 0) == 0 else max(1, min(6, int(override)))
    return max(1, min(requested, max(1, int(page_count))))


class ParallelPageScheduler:
    """Dynamically schedule translation and release results in page order.

    The combined active/reorder window is bounded to ``workers * 2``. Jobs
    outside that window remain lightweight immutable descriptors.
    """

    def __init__(
        self,
        workers: int,
        *,
        gpu_state: str,
        cancel_event: threading.Event,
        snapshot_callback: Callable[[SchedulerSnapshot], None] | None = None,
    ) -> None:
        self.workers = max(1, min(6, int(workers)))
        self.window = self.workers * 2
        self.gpu_state = str(gpu_state)
        self.cancel_event = cancel_event
        self.snapshot_callback = snapshot_callback

    def run(
        self,
        jobs: list[ParallelPageJob],
        stage: PageTranslationStage,
        commit: Callable[[PageTranslationOutcome], PageTranslationOutcome | None],
    ) -> list[PageTranslationOutcome]:
        ordered = sorted(jobs, key=lambda item: item.page_index)
        if not ordered:
            return []
        total = len(ordered)
        completed = failed = 0
        next_submit = 0
        next_commit = 0
        active: dict[Future[PageTranslationOutcome], ParallelPageJob] = {}
        buffered: dict[int, PageTranslationOutcome] = {}
        committed: list[PageTranslationOutcome] = []

        def publish() -> None:
            if self.snapshot_callback is not None:
                gpu_state = (
                    "Active"
                    if self.gpu_state in {"Idle", "Configured"} and active
                    else self.gpu_state
                )
                self.snapshot_callback(SchedulerSnapshot(
                    configured_workers=self.workers,
                    active_workers=len(active),
                    queued=max(0, total - next_submit),
                    completed=completed,
                    failed=failed,
                    total=total,
                    gpu_state=gpu_state,
                ))

        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="HydraFastPage") as executor:
            while next_commit < total:
                while (
                    not self.cancel_event.is_set()
                    and next_submit < total
                    and len(active) < self.workers
                    and len(active) + len(buffered) < self.window
                ):
                    job = ordered[next_submit]
                    active[executor.submit(stage, job)] = job
                    next_submit += 1
                    publish()

                expected = ordered[next_commit]
                outcome = buffered.pop(expected.page_index, None)
                if outcome is not None:
                    committed_outcome = commit(outcome) or outcome
                    committed.append(committed_outcome)
                    completed += int(committed_outcome.succeeded)
                    failed += int(not committed_outcome.succeeded and not committed_outcome.cancelled)
                    next_commit += 1
                    publish()
                    continue

                if not active:
                    break

                done, _ = wait(tuple(active), return_when=FIRST_COMPLETED)
                for future in done:
                    job = active.pop(future)
                    try:
                        result = future.result()
                    except BaseException as error:
                        result = PageTranslationOutcome(
                            page_index=job.page_index,
                            image_id=job.image_id,
                            request_id=job.request_id,
                            attempts=1,
                            error=f"{type(error).__name__}: {error}",
                        )
                    buffered[result.page_index] = result
                publish()

            # Running calls are allowed to settle, but cancellation only commits
            # the already contiguous prefix. Executor shutdown waits cleanly.
            if self.cancel_event.is_set():
                for future in tuple(active):
                    future.cancel()

        publish()
        return committed


def timed_stage(
    job: ParallelPageJob,
    translate: Callable[[PageDialogue], tuple[PageTranslation, str, int]],
) -> PageTranslationOutcome:
    started = time.perf_counter()
    try:
        if job.page is None:
            raise ValueError("Translation stage received an unhydrated page job")
        result, provider_id, attempts = translate(job.page)
    except BaseException as error:
        return PageTranslationOutcome(
            page_index=job.page_index,
            image_id=job.image_id,
            request_id=job.request_id,
            attempts=int(getattr(error, "attempts", 1) or 1),
            elapsed_seconds=time.perf_counter() - started,
            error=f"{type(error).__name__}: {error}",
        )
    return PageTranslationOutcome(
        page_index=job.page_index,
        image_id=job.image_id,
        request_id=job.request_id,
        translation=result,
        provider_id=provider_id,
        attempts=attempts,
        elapsed_seconds=time.perf_counter() - started,
    )
