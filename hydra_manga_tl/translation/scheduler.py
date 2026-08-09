"""Bounded, ordered page translation scheduling for Fast mode.

Compatibility facade for the split scheduler modules.
"""

from __future__ import annotations

from hydra_manga_tl.translation.scheduler_models import (
    BubbleTranslationUnit,
    DEFAULT_PROVIDER_PROFILES,
    PageTranslationOutcome,
    ParallelPageJob,
    ProviderDispatcher,
    ProviderProfile,
    SchedulerSnapshot,
    SmartPageJob,
    TokenBudgetManager,
    TranslationQueueItem,
    TranslationResultStore,
    resolve_provider_worker_count,
)
from hydra_manga_tl.translation.scheduler_parallel import (
    PageTranslationStage,
    ParallelPageScheduler,
    auto_worker_count,
    resolve_worker_count,
    timed_stage,
)
from hydra_manga_tl.translation.scheduler_smart import (
    PROVIDER_FUTURE_POLL_SECONDS,
    SmartTranslationScheduler,
)
from hydra_manga_tl.translation.memory import TRANSLATION_MEMORY

__all__ = [
    "BubbleTranslationUnit",
    "DEFAULT_PROVIDER_PROFILES",
    "PROVIDER_FUTURE_POLL_SECONDS",
    "PageTranslationOutcome",
    "PageTranslationStage",
    "ParallelPageJob",
    "ParallelPageScheduler",
    "ProviderDispatcher",
    "ProviderProfile",
    "SchedulerSnapshot",
    "SmartPageJob",
    "SmartTranslationScheduler",
    "TokenBudgetManager",
    "TRANSLATION_MEMORY",
    "TranslationQueueItem",
    "TranslationResultStore",
    "auto_worker_count",
    "resolve_provider_worker_count",
    "resolve_worker_count",
    "timed_stage",
]
