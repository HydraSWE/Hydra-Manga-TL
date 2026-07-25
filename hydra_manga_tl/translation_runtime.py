"""Application-lifetime translation manager and background model warmup."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from .translation import MarianTranslator
from .translation_engines import PageDialogue, PageTranslation, TranslationEngineManager


LOGGER = logging.getLogger(__name__)
_WARMUP_THREADS: list[threading.Thread] = []
_LOCK = threading.RLock()


def _freeze_mapping(value: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(key), str(item)) for key, item in (value or {}).items()))


@dataclass(frozen=True)
class TranslationRuntimeConfig:
    preferred_engine: str = "qwen"
    fallback_engine: str = "marian"
    qwen_model_path: str = ""
    qwen_model_name: str = "Qwen3-4B-Instruct-2507"
    glossary: tuple[tuple[str, str], ...] = ()
    provider_models: tuple[tuple[str, str], ...] = ()
    allow_local_fallback_for_cloud: bool = False

    @classmethod
    def from_mapping(cls, config: dict[str, Any] | None) -> "TranslationRuntimeConfig":
        values = config or {}
        return cls(
            preferred_engine=str(values.get("translation_engine") or "qwen").strip().lower(),
            fallback_engine=str(values.get("translation_fallback_engine", "marian")).strip().lower(),
            qwen_model_path=str(values.get("qwen_model_path") or values.get("qwen_model") or ""),
            qwen_model_name=str(values.get("qwen_model_name") or "Qwen3-4B-Instruct-2507"),
            glossary=_freeze_mapping(values.get("glossary")),
            provider_models=_freeze_mapping(values.get("provider_models")),
            allow_local_fallback_for_cloud=bool(values.get("allow_local_fallback_for_cloud", False)),
        )


class TranslationRuntime:
    """Serialize access to exactly one configured translation manager."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._manager: TranslationEngineManager | None = None
        self._config: TranslationRuntimeConfig | None = None
        self._generation = 0

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def manager(self) -> TranslationEngineManager | None:
        with self._lock:
            return self._manager

    @property
    def last_engine_id(self) -> str:
        with self._lock:
            return str(getattr(self._manager, "last_engine_id", "")) if self._manager else ""

    def translate_page(
        self,
        page: PageDialogue,
        config: dict[str, Any] | TranslationRuntimeConfig | None = None,
    ) -> PageTranslation:
        runtime_config = (
            config if isinstance(config, TranslationRuntimeConfig)
            else TranslationRuntimeConfig.from_mapping(config)
        )
        with self._lock:
            manager = self._ensure_manager(runtime_config)
            return manager.translate_page(page)

    def _ensure_manager(self, config: TranslationRuntimeConfig) -> TranslationEngineManager:
        if self._manager is not None and self._config == config:
            return self._manager
        previous = self._manager
        manager = TranslationEngineManager(
            glossary=dict(config.glossary),
            qwen_model_path=config.qwen_model_path or None,
            preferred_engine=config.preferred_engine,
            fallback_engine=config.fallback_engine,
            qwen_model_name=config.qwen_model_name,
            provider_models=dict(config.provider_models),
            allow_local_fallback_for_cloud=config.allow_local_fallback_for_cloud,
        )
        manager.load()
        self._manager = manager
        self._config = config
        self._generation += 1
        if previous is not None:
            previous.unload()
        return manager

    def unload(self) -> None:
        with self._lock:
            if self._manager is not None:
                self._manager.unload()
            self._manager = None
            self._config = None


TRANSLATION_RUNTIME = TranslationRuntime()


def start_translation_warmup(
    source_language: str = "Japanese",
    target_language: str = "en",
    *,
    translation_engine: str = "marian",
) -> None:
    # Cloud engines and Qwen are intentionally lazy. In particular, a cloud
    # selection must not import/load Torch through Marian at application start.
    if str(translation_engine or "").strip().lower() != "marian":
        return
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


def shutdown_translation_runtime() -> None:
    TRANSLATION_RUNTIME.unload()


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
