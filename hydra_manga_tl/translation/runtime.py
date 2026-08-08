"""Application-lifetime translation manager and background model warmup."""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from urllib.error import URLError

from hydra_manga_tl.translation.service import MarianTranslator, TranslationHTTPError
from hydra_manga_tl.translation.engines import PageDialogue, PageTranslation, TranslationEngineManager
from hydra_manga_tl.translation.engines.registry import TRANSLATION_PROVIDER_REGISTRY


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
    provider_base_urls: tuple[tuple[str, str], ...] = ()
    allow_local_fallback_for_cloud: bool = False
    translation_memory_enabled: bool = True
    translation_memory_prefer_verified: bool = True

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
            provider_base_urls=_freeze_mapping(values.get("provider_base_urls")),
            allow_local_fallback_for_cloud=bool(values.get("allow_local_fallback_for_cloud", False)),
            translation_memory_enabled=bool(
                values.get("translation_memory_enabled", True)
            ),
            translation_memory_prefer_verified=bool(
                values.get("translation_memory_prefer_verified", True)
            ),
        )


class TranslationRuntime:
    """Serialize access to exactly one configured translation manager."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._manager: TranslationEngineManager | None = None
        self._config: TranslationRuntimeConfig | None = None
        self._manager_loaded = False
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
            if not self._manager_loaded:
                manager.load()
                self._manager_loaded = True
            return manager.translate_page(page)

    def translate_cached_page(
        self,
        page: PageDialogue,
        cached: PageTranslation,
        config: dict[str, Any] | TranslationRuntimeConfig | None = None,
    ) -> PageTranslation:
        runtime_config = (
            config if isinstance(config, TranslationRuntimeConfig)
            else TranslationRuntimeConfig.from_mapping(config)
        )
        with self._lock:
            manager = self._ensure_manager(runtime_config)
            return manager.translate_cached_page(page, cached)

    def _ensure_manager(self, config: TranslationRuntimeConfig) -> TranslationEngineManager:
        if self._manager is not None and self._config == config:
            return self._manager
        previous = self._manager
        manager = _create_manager(config)
        self._manager = manager
        self._config = config
        self._manager_loaded = False
        self._generation += 1
        if previous is not None:
            previous.unload()
        return manager

    def warm(self, config: dict[str, Any] | TranslationRuntimeConfig | None = None) -> bool:
        runtime_config = (
            config if isinstance(config, TranslationRuntimeConfig)
            else TranslationRuntimeConfig.from_mapping(config)
        )
        with self._lock:
            manager = self._ensure_manager(runtime_config)
            warmed = manager.load()
            self._manager_loaded = bool(warmed)
            return warmed

    def unload(self) -> None:
        with self._lock:
            if self._manager is not None:
                self._manager.unload()
            self._manager = None
            self._config = None
            self._manager_loaded = False

    def fast_session(
        self,
        config: dict[str, Any] | TranslationRuntimeConfig | None = None,
        *,
        cancel_event: threading.Event | None = None,
    ) -> "FastTranslationSession":
        runtime_config = (
            config if isinstance(config, TranslationRuntimeConfig)
            else TranslationRuntimeConfig.from_mapping(config)
        )
        with self._lock:
            manager = self._ensure_manager(runtime_config)
        return FastTranslationSession(
            manager,
            runtime_config,
            cancel_event=cancel_event,
        )


def _terminal_provider_error(error: BaseException) -> bool:
    if isinstance(error, TranslationHTTPError):
        return error.status in {400, 401, 403, 404, 405, 409, 410, 422}
    message = f"{type(error).__name__}: {error}".casefold()
    markers = (
        "api key", "authentication", "unauthorized", "forbidden", "credential",
        "model not found", "model not installed", "unsupported local translation pair",
        "unknown translation engine", "invalid configuration",
    )
    return isinstance(error, (FileNotFoundError, PermissionError)) or any(
        marker in message for marker in markers
    )


def _transient_provider_error(error: BaseException) -> bool:
    if isinstance(error, TranslationHTTPError):
        return error.status in {408, 425, 429, 500, 502, 503, 504}
    if isinstance(error, (TimeoutError, ConnectionError, URLError)):
        return True
    return not _terminal_provider_error(error)


def _retry_delay(error: BaseException, attempt: int) -> float:
    if isinstance(error, TranslationHTTPError) and error.retry_after is not None:
        return min(30.0, max(0.0, error.retry_after))
    base = min(4.0, 0.5 * (2 ** max(0, attempt - 1)))
    return base + random.uniform(0.0, base * 0.25)


class FastTranslationSession:
    """Batch-scoped provider health and local inference serialization."""

    def __init__(
        self,
        manager: TranslationEngineManager,
        config: TranslationRuntimeConfig,
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.manager = manager
        self.primary = config.preferred_engine
        self.fallback = config.fallback_engine if config.fallback_engine != config.preferred_engine else ""
        self._health_lock = threading.RLock()
        self._unhealthy: set[str] = set()
        self._inference_locks: dict[str, threading.RLock] = {}
        self._cancel_event = cancel_event

    @property
    def gpu_state(self) -> str:
        registration = TRANSLATION_PROVIDER_REGISTRY.get(self.primary)
        if registration is None or registration.cloud:
            return "Cloud / Not Used"
        try:
            from hydra_manga_tl.core.gpu import translation_gpu_state

            return translation_gpu_state(
                self.primary,
                engine=self.manager.engines.get(self.primary),
            )
        except Exception:
            return "Detection error"

    def _call(self, engine_key: str, page: PageDialogue) -> tuple[PageTranslation, str]:
        registration = TRANSLATION_PROVIDER_REGISTRY[engine_key]
        if registration.serialized_inference:
            with self._health_lock:
                lock = self._inference_locks.setdefault(engine_key, threading.RLock())
            with lock:
                return self.manager.translate_page_using(engine_key, page)
        return self.manager.translate_page_using(engine_key, page)

    def translate_page(self, page: PageDialogue) -> tuple[PageTranslation, str, int]:
        attempts = 0
        primary_error: BaseException | None = None
        with self._health_lock:
            primary_healthy = self.primary not in self._unhealthy
        if primary_healthy:
            for attempt in range(1, 3):
                attempts += 1
                try:
                    result, provider = self._call(self.primary, page)
                    return result, provider, attempts
                except Exception as error:
                    primary_error = error
                    if _terminal_provider_error(error):
                        with self._health_lock:
                            self._unhealthy.add(self.primary)
                        break
                    if attempt >= 2 or not _transient_provider_error(error):
                        break
                    delay = _retry_delay(error, attempt)
                    LOGGER.warning(
                        "Transient translation failure provider=%s attempt=%d "
                        "retry_in=%.2fs error=%s",
                        self.primary,
                        attempt,
                        delay,
                        error,
                    )
                    if self._cancel_event is not None:
                        if self._cancel_event.wait(delay):
                            raise RuntimeError(
                                "Translation request was cancelled during retry backoff"
                            ) from error
                    else:
                        time.sleep(delay)
        if self.fallback:
            with self._health_lock:
                fallback_healthy = self.fallback not in self._unhealthy
            if fallback_healthy:
                attempts += 1
                try:
                    result, provider = self._call(self.fallback, page)
                    return result, provider, attempts
                except Exception as error:
                    if _terminal_provider_error(error):
                        with self._health_lock:
                            self._unhealthy.add(self.fallback)
                    failure = RuntimeError(
                        f"Translation engine '{self.primary}' failed: {primary_error}; "
                        f"fallback engine '{self.fallback}' also failed: {error}"
                    )
                    failure.attempts = attempts  # type: ignore[attr-defined]
                    raise failure from error
        if primary_error is not None:
            failure = RuntimeError(
                f"Translation engine '{self.primary}' failed after retry: {primary_error}"
            )
            failure.attempts = attempts  # type: ignore[attr-defined]
            raise failure from primary_error
        failure = RuntimeError(
            f"Translation engine '{self.primary}' is unavailable and no healthy fallback is configured"
        )
        failure.attempts = attempts  # type: ignore[attr-defined]
        raise failure

    def translate_cached_page(
        self,
        page: PageDialogue,
        cached: PageTranslation,
    ) -> tuple[PageTranslation, str, int]:
        return self.manager.translate_cached_page(page, cached), "page-cache", 0


TRANSLATION_RUNTIME = TranslationRuntime()


def start_translation_warmup(
    source_language: str = "Japanese",
    target_language: str = "en",
    *,
    translation_engine: str = "marian",
    config: dict[str, Any] | TranslationRuntimeConfig | None = None,
) -> None:
    engine = str(translation_engine or "").strip().lower()
    if engine == "qwen":
        runtime_config = (
            config if isinstance(config, TranslationRuntimeConfig)
            else TranslationRuntimeConfig.from_mapping(config)
        )
        if not runtime_config.qwen_model_path or not Path(runtime_config.qwen_model_path).exists():
            LOGGER.info("Skipping Qwen warmup because its configured model path is unavailable")
            return
        key = ("runtime", runtime_config)
        target = _warm_runtime
        args = (runtime_config,)
    elif engine == "marian":
        key = ("marian", source_language, target_language)
        target = _warm_marian
        args = (source_language, target_language)
    else:
        # Cloud engines stay lazy: startup performs only local credential/model
        # validation and must never make network requests.
        return

    with _LOCK:
        _WARMUP_THREADS[:] = [thread for thread in _WARMUP_THREADS if thread.is_alive()]
        if any(thread.is_alive() and getattr(thread, "_hydra_translation_key", None) == key for thread in _WARMUP_THREADS):
            return
        thread = threading.Thread(
            target=target,
            args=args,
            name=f"HydraTranslationWarmup-{engine}",
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
    with _LOCK:
        _WARMUP_THREADS[:] = [thread for thread in _WARMUP_THREADS if thread.is_alive()]


def shutdown_translation_runtime() -> None:
    TRANSLATION_RUNTIME.unload()


def _warm_runtime(config: TranslationRuntimeConfig) -> None:
    started = time.perf_counter()
    try:
        warmed = TRANSLATION_RUNTIME.warm(config)
        if not warmed:
            LOGGER.warning(
                "Translation runtime warmup failed for %s in %.2fs; "
                "requests will use fallback when available",
                config.preferred_engine,
                time.perf_counter() - started,
            )
            return
        LOGGER.info(
            "Translation runtime warmup finished for %s in %.2fs",
            config.preferred_engine,
            time.perf_counter() - started,
        )
    except Exception:
        LOGGER.exception("Translation runtime warmup failed for %s", config.preferred_engine)


def _create_manager(config: TranslationRuntimeConfig) -> TranslationEngineManager:
    return TranslationEngineManager(
        glossary=dict(config.glossary),
        qwen_model_path=config.qwen_model_path or None,
        preferred_engine=config.preferred_engine,
        fallback_engine=config.fallback_engine,
        qwen_model_name=config.qwen_model_name,
        provider_models=dict(config.provider_models),
        provider_base_urls=dict(config.provider_base_urls),
        allow_local_fallback_for_cloud=config.allow_local_fallback_for_cloud,
        translation_memory_enabled=config.translation_memory_enabled,
        translation_memory_prefer_verified=(
            config.translation_memory_prefer_verified
        ),
    )


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
