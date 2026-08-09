"""Translation scheduler data contracts and provider helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hydra_manga_tl.core.normalization import normalize_global_text
from hydra_manga_tl.translation.engines import PageDialogue, PageTranslation


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


