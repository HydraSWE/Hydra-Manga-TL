"""Background translation model warmup."""

from __future__ import annotations

import logging
import threading
import time

from .translation import MarianTranslator


LOGGER = logging.getLogger(__name__)
_WARMUP_THREADS: list[threading.Thread] = []
_LOCK = threading.RLock()


def start_translation_warmup(source_language: str = "Japanese", target_language: str = "en") -> None:
    key = (source_language, target_language)
    with _LOCK:
        if any(thread.is_alive() and getattr(thread, "_hydra_translation_key", None) == key for thread in _WARMUP_THREADS):
            return
        thread = threading.Thread(
            target=_warm_marian,
            args=key,
            name=f"HydraTranslationWarmup-{source_language}-{target_language}",
            daemon=True,
        )
        thread._hydra_translation_key = key  # type: ignore[attr-defined]
        _WARMUP_THREADS.append(thread)
        thread.start()


def wait_for_translation_warmup(timeout: float | None = None) -> None:
    deadline = None if timeout is None else time.perf_counter() + timeout
    for thread in list(_WARMUP_THREADS):
        remaining = None if deadline is None else max(0.0, deadline - time.perf_counter())
        thread.join(remaining)


def shutdown_translation_warmup(timeout: float = 1.0) -> None:
    wait_for_translation_warmup(timeout)


def _warm_marian(source_language: str, target_language: str) -> None:
    started = time.perf_counter()
    try:
        MarianTranslator().translate(["テスト"], source_language, target_language)
        LOGGER.info(
            "Translation warmup finished for %s->%s in %.2fs",
            source_language,
            target_language,
            time.perf_counter() - started,
        )
    except Exception:
        LOGGER.exception("Translation warmup failed for %s->%s", source_language, target_language)
