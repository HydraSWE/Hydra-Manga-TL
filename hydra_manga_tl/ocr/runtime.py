"""Shared OCR runtime warmup for the desktop pipeline."""

from __future__ import annotations

import logging
import multiprocessing
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from hydra_manga_tl.core.fonts import find_font_file
from hydra_manga_tl.ocr.core import PaddleOCREngine
from hydra_manga_tl.ocr.worker import run_ocr_worker


LOGGER = logging.getLogger(__name__)
DEFAULT_WARMUP_LANGUAGES = ("japan",)
DEFAULT_WORKER_STARTUP_TIMEOUT = 420.0
BACKGROUND_WARMUP_OBSERVE_TIMEOUT = 15.0
OCR_WORKER_ENVIRONMENT = {
    "FLAGS_use_mkldnn": "0",
    "KMP_DUPLICATE_LIB_OK": "TRUE",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
}

_ENGINES: dict[tuple[str, ...], PaddleOCREngine] = {}
_WARMUP_THREADS: list[threading.Thread] = []
_LOCK = threading.RLock()


class OCRWorkerError(RuntimeError):
    pass


class OCRWorkerCrashed(OCRWorkerError):
    pass


class OCRRuntimeState(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    LOADING_MODEL = "LOADING_MODEL"
    WARMING = "WARMING"
    READY = "READY"
    BUSY = "BUSY"
    FAILED = "FAILED"
    RESTARTING = "RESTARTING"


@dataclass
class OCRRuntimeMetrics:
    worker_pid: int | None = None
    runtime_state: str = OCRRuntimeState.STOPPED
    warmup_time: float = 0.0
    pages_processed: int = 0
    average_ocr_time: float = 0.0
    peak_ocr_time: float = 0.0
    retry_count: int = 0
    current_memory: float = 0.0
    peak_memory: float = 0.0
    restart_count: int = 0
    recycle_pages: int = 25

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OCRWorkerClient:
    """Single crash-contained OCR process with application lifetime ownership."""

    def __init__(
        self,
        *,
        languages: tuple[str, ...] = DEFAULT_WARMUP_LANGUAGES,
        memory_limit_mb: int = 2048,
        recycle_pages: int = 25,
        request_timeout: float = 900.0,
        startup_timeout: float = DEFAULT_WORKER_STARTUP_TIMEOUT,
    ) -> None:
        self.languages = _language_key(languages) or DEFAULT_WARMUP_LANGUAGES
        self.memory_limit_mb = max(0, int(memory_limit_mb))
        self.recycle_pages = max(1, int(recycle_pages))
        self.request_timeout = float(request_timeout)
        self.startup_timeout = float(startup_timeout)
        self.restart_count = 0
        self.pages_processed = 0
        self.worker_rss_mb = 0.0
        self.warmup_time = 0.0
        self.total_ocr_time = 0.0
        self.peak_ocr_time = 0.0
        self.retry_count = 0
        self.peak_worker_rss_mb = 0.0
        self.state = OCRRuntimeState.STOPPED
        self._process = None
        self._connection = None
        self._lock = threading.RLock()

    @property
    def alive(self) -> bool:
        return bool(self._process is not None and self._process.is_alive())

    def _set_state(self, state: str | OCRRuntimeState) -> None:
        try:
            self.state = OCRRuntimeState(str(state))
        except ValueError:
            self.state = OCRRuntimeState.FAILED

    def start(self) -> None:
        with self._lock:
            if self.alive:
                return
            self._start(restart=self._process is not None)

    def _start(self, *, restart: bool = False) -> None:
        self._set_state(OCRRuntimeState.RESTARTING if restart else OCRRuntimeState.STARTING)
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe()
        process = context.Process(
            target=run_ocr_worker,
            args=(child_connection, self.languages),
            name="HydraOCRWorker",
            daemon=True,
        )
        previous_environment = {key: os.environ.get(key) for key in OCR_WORKER_ENVIRONMENT}
        os.environ.update(OCR_WORKER_ENVIRONMENT)
        try:
            process.start()
        finally:
            for key, value in previous_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        child_connection.close()
        self._connection = parent_connection
        self._process = process
        self.pages_processed = 0
        self.worker_rss_mb = 0.0
        if restart:
            self.restart_count += 1

    def _receive_until_response(self, timeout: float) -> dict:
        deadline = time.perf_counter() + timeout
        while True:
            remaining = max(0.0, deadline - time.perf_counter())
            if not self._connection.poll(remaining):
                self._restart()
                raise OCRWorkerCrashed("OCR worker timed out and was restarted")
            try:
                response = self._connection.recv()
            except (EOFError, BrokenPipeError, OSError) as error:
                crash = self._crash_summary(error)
                self._restart()
                raise OCRWorkerCrashed(f"OCR worker exited unexpectedly: {crash}") from error
            self._record_response(response)
            if response.get("event") == "state":
                continue
            return response

    def _wait_until_ready(self, timeout: float | None = None, *, restart_on_timeout: bool = True) -> None:
        if self.state == OCRRuntimeState.READY:
            return
        deadline = time.perf_counter() + (self.startup_timeout if timeout is None else timeout)
        while self.state not in {OCRRuntimeState.READY, OCRRuntimeState.FAILED, OCRRuntimeState.STOPPED}:
            remaining = max(0.0, deadline - time.perf_counter())
            if remaining <= 0:
                if restart_on_timeout:
                    self._restart()
                    raise OCRWorkerCrashed("OCR worker warm-up timed out and was restarted")
                raise OCRWorkerCrashed("OCR worker is still warming up")
            if not self._connection.poll(min(remaining, 0.25)):
                if not self.alive:
                    if restart_on_timeout:
                        self._restart()
                    raise OCRWorkerCrashed("OCR worker exited during warm-up")
                continue
            try:
                response = self._connection.recv()
            except (EOFError, BrokenPipeError, OSError) as error:
                crash = self._crash_summary(error)
                if restart_on_timeout:
                    self._restart()
                raise OCRWorkerCrashed(f"OCR worker exited during warm-up: {crash}") from error
            self._record_response(response)
            if response.get("event") != "state":
                if not response.get("ok", True):
                    error = str(response.get("error") or "OCR worker failed during warm-up")
                    self._set_state(OCRRuntimeState.FAILED)
                    if restart_on_timeout:
                        self._restart()
                    raise OCRWorkerCrashed(_worker_error_message(error))
                return
        if self.state != OCRRuntimeState.READY:
            raise OCRWorkerCrashed(f"OCR worker is not ready: {self.state}")

    def _record_response(self, response: dict) -> None:
        self.worker_rss_mb = float(response.get("rss_mb", self.worker_rss_mb) or 0.0)
        self.peak_worker_rss_mb = max(self.peak_worker_rss_mb, self.worker_rss_mb)
        if "state" in response:
            self._set_state(response["state"])
        if response.get("warmup_seconds") is not None:
            self.warmup_time = float(response.get("warmup_seconds") or 0.0)

    def request(self, command: str, request: dict) -> dict:
        with self._lock:
            if not self.alive:
                self._start(restart=self._process is not None)
            elif self.state == OCRRuntimeState.STOPPED:
                self._set_state(OCRRuntimeState.READY)
            self._wait_until_ready()
            request_started = time.perf_counter()
            try:
                self._set_state(OCRRuntimeState.BUSY)
                self._connection.send({"command": command, **request})
                response = self._receive_until_response(self.request_timeout)
            except (EOFError, BrokenPipeError, OSError) as error:
                crash = self._crash_summary(error)
                self._restart()
                raise OCRWorkerCrashed(f"OCR worker exited unexpectedly: {crash}") from error
            finally:
                if self.alive and self.state == OCRRuntimeState.BUSY:
                    self._set_state(OCRRuntimeState.READY)
            if not response.get("ok"):
                self._set_state(OCRRuntimeState.FAILED)
                raise OCRWorkerError(str(response.get("error", "OCR worker failed")))
            self.pages_processed += 1
            elapsed = time.perf_counter() - request_started
            self.total_ocr_time += elapsed
            self.peak_ocr_time = max(self.peak_ocr_time, elapsed)
            manager = dict(response.get("ocr_result", {}).get("metadata", {}).get("manager", {}))
            self.retry_count += int(manager.get("retry_summary", {}).get("attempt_count", 0) or 0)
            if (
                self.memory_limit_mb
                and self.pages_processed >= self.recycle_pages
                and self.worker_rss_mb >= self.memory_limit_mb
            ):
                self._restart()
            return response

    def _crash_summary(self, error: BaseException) -> str:
        process = self._process
        parts = [type(error).__name__]
        if str(error):
            parts.append(str(error))
        if process is not None:
            parts.append(f"pid={process.pid}")
            parts.append(f"exitcode={process.exitcode}")
        parts.append(f"state={self.state}")
        return "; ".join(parts)

    def analyze_page(self, request: dict) -> dict:
        return self.request("analyze_page", request)

    def analyze_selection(self, request: dict) -> dict:
        return self.request("analyze_selection", request)

    def ping(self, timeout: float = 10.0, *, restart_on_timeout: bool = True) -> bool:
        with self._lock:
            if not self.alive:
                self._start(restart=self._process is not None)
            try:
                self._wait_until_ready(timeout, restart_on_timeout=restart_on_timeout)
                self._connection.send({"command": "ping"})
                response = self._receive_until_response(timeout)
            except (EOFError, BrokenPipeError, OSError):
                self._restart()
                return False
            except OCRWorkerError:
                if restart_on_timeout:
                    raise
                return False
            return bool(response.get("ok"))

    def restart(self) -> None:
        with self._lock:
            self._restart()

    def _restart(self) -> None:
        self._set_state(OCRRuntimeState.RESTARTING)
        self.close(force=True)
        self._start(restart=True)

    def metrics(self) -> dict[str, Any]:
        process = self._process
        average = self.total_ocr_time / self.pages_processed if self.pages_processed else 0.0
        return OCRRuntimeMetrics(
            worker_pid=process.pid if process is not None else None,
            runtime_state=str(self.state),
            warmup_time=round(self.warmup_time, 3),
            pages_processed=self.pages_processed,
            average_ocr_time=round(average, 3),
            peak_ocr_time=round(self.peak_ocr_time, 3),
            retry_count=self.retry_count,
            current_memory=round(self.worker_rss_mb, 2),
            peak_memory=round(self.peak_worker_rss_mb, 2),
            restart_count=self.restart_count,
            recycle_pages=self.recycle_pages,
        ).to_dict()

    def close(self, *, force: bool = False) -> None:
        process, connection = self._process, self._connection
        self._process = None
        self._connection = None
        if force and process is not None and process.is_alive():
            process.terminate()
        if connection is not None:
            if not force and process is not None and process.is_alive():
                try:
                    connection.send({"command": "shutdown"})
                except (BrokenPipeError, OSError):
                    pass
            try:
                connection.close()
            except OSError:
                pass
        if process is not None:
            process.join(2.0)
            if process.is_alive():
                if not force:
                    process.terminate()
                    process.join(2.0)
            if process.is_alive():
                LOGGER.warning(
                    "OCR worker pid=%s survived terminate; escalating shutdown",
                    getattr(process, "pid", None),
                )
                kill = getattr(process, "kill", None)
                if callable(kill):
                    kill()
                    process.join(1.0)
            if process.is_alive():
                LOGGER.error(
                    "OCR worker pid=%s is still alive after shutdown escalation",
                    getattr(process, "pid", None),
                )
            else:
                LOGGER.info(
                    "OCR worker stopped pid=%s exitcode=%s",
                    getattr(process, "pid", None),
                    getattr(process, "exitcode", None),
                )
        self._set_state(OCRRuntimeState.STOPPED)


class OCRRuntimeManager:
    """Application singleton that owns the persistent OCR worker and queue."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._client: OCRWorkerClient | None = None
        self._warmup_thread: threading.Thread | None = None
        self._memory_limit_mb = 2048
        self._recycle_pages = 25

    def configure(self, *, memory_limit_mb: int = 2048, recycle_pages: int = 25) -> None:
        with self._lock:
            self._memory_limit_mb = max(0, int(memory_limit_mb))
            self._recycle_pages = max(1, int(recycle_pages))
            if self._client is not None:
                self._client.memory_limit_mb = self._memory_limit_mb
                self._client.recycle_pages = self._recycle_pages

    def start(self) -> None:
        with self._lock:
            if self._client is None:
                self._client = OCRWorkerClient(
                    memory_limit_mb=self._memory_limit_mb,
                    recycle_pages=self._recycle_pages,
                )
            self._client.start()

    def start_warmup(self) -> None:
        with self._lock:
            self.start()
            if self._warmup_thread is not None and self._warmup_thread.is_alive():
                return
            self._warmup_thread = threading.Thread(
                target=self._warmup_client,
                name="HydraOCRWorkerWarmup",
                daemon=True,
            )
            self._warmup_thread.start()

    def _warmup_client(self) -> None:
        try:
            client = self.client()
            if client.ping(
                timeout=min(BACKGROUND_WARMUP_OBSERVE_TIMEOUT, client.startup_timeout),
                restart_on_timeout=False,
            ):
                LOGGER.info("OCR worker warmup finished in %.2fs", client.warmup_time)
            else:
                LOGGER.info("OCR worker is still warming up; first OCR request will wait for it")
        except OCRWorkerError:
            LOGGER.exception("OCR worker warmup failed; it will retry on first OCR request")

    def client(self) -> OCRWorkerClient:
        with self._lock:
            if self._client is None:
                self._client = OCRWorkerClient(
                    memory_limit_mb=self._memory_limit_mb,
                    recycle_pages=self._recycle_pages,
                )
            self._client.start()
            return self._client

    def shutdown(self) -> None:
        warmup_thread: threading.Thread | None = None
        with self._lock:
            if self._warmup_thread is not None and self._warmup_thread.is_alive():
                warmup_thread = self._warmup_thread
            self._warmup_thread = None
        if warmup_thread is not None:
            warmup_thread.join(1.0)
        with self._lock:
            if self._client is not None:
                self._client.close()
            self._client = None

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            if self._client is None:
                return OCRRuntimeMetrics().to_dict()
            return self._client.metrics()


OCR_RUNTIME = OCRRuntimeManager()


def start_ocr_runtime(*, memory_limit_mb: int = 2048, recycle_pages: int = 25) -> None:
    OCR_RUNTIME.configure(memory_limit_mb=memory_limit_mb, recycle_pages=recycle_pages)
    OCR_RUNTIME.start_warmup()


def get_ocr_runtime_client() -> OCRWorkerClient:
    return OCR_RUNTIME.client()


def get_ocr_runtime_metrics() -> dict[str, Any]:
    return OCR_RUNTIME.metrics()


def shutdown_ocr_runtime() -> None:
    OCR_RUNTIME.shutdown()


def _language_key(languages: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(language) for language in languages if language))


def _worker_error_message(error: str) -> str:
    lowered = error.lower()
    if "warm" in lowered:
        return error
    return f"OCR worker failed during warm-up: {error}"


def get_ocr_engine(languages: tuple[str, ...] | list[str]) -> PaddleOCREngine:
    key = _language_key(languages)
    warmup_thread: threading.Thread | None = None
    with _LOCK:
        engine = _ENGINES.get(key)
        if engine is not None:
            return engine
        warmup_thread = next((
            thread for thread in _WARMUP_THREADS
            if thread.is_alive() and getattr(thread, "_hydra_ocr_key", None) == key
        ), None)
    if warmup_thread is not None:
        warmup_thread.join()
        with _LOCK:
            engine = _ENGINES.get(key)
            if engine is not None:
                return engine
    with _LOCK:
        engine = _ENGINES.get(key)
        if engine is not None:
            return engine
        engine = PaddleOCREngine(list(key))
        _ENGINES[key] = engine
        return engine


def get_ocr_engine_for_language(language: str, fallback_languages: tuple[str, ...] | list[str] = DEFAULT_WARMUP_LANGUAGES) -> PaddleOCREngine:
    """Return an existing shared wrapper that can serve language before creating one.

    PaddleX/PaddleOCR is touchy about repeated initialization in one process.
    Manual OCR boxes often need only the same Japanese wrapper the main page
    pipeline warmed or used, so prefer any compatible cached wrapper first.
    """
    language = str(language or "").strip()
    fallback_key = _language_key(fallback_languages)
    with _LOCK:
        for key, engine in _ENGINES.items():
            if language and language in key:
                return engine
        for key, engine in _ENGINES.items():
            if fallback_key and all(item in key for item in fallback_key):
                return engine
    return get_ocr_engine((language,) if language else fallback_key)


def start_ocr_warmup(languages: tuple[str, ...] | list[str] = DEFAULT_WARMUP_LANGUAGES) -> None:
    key = _language_key(languages)
    with _LOCK:
        _WARMUP_THREADS[:] = [thread for thread in _WARMUP_THREADS if thread.is_alive()]
        if key in _ENGINES:
            return
        if any(thread.is_alive() and getattr(thread, "_hydra_ocr_key", None) == key for thread in _WARMUP_THREADS):
            return
        thread = threading.Thread(target=_warm_ocr_engine, args=(key,), name=f"HydraOCRWarmup-{'+'.join(key)}", daemon=True)
        thread._hydra_ocr_key = key  # type: ignore[attr-defined]
        _WARMUP_THREADS.append(thread)
        thread.start()


def wait_for_ocr_warmup(timeout: float | None = None) -> bool:
    deadline = None if timeout is None else time.perf_counter() + timeout
    for thread in list(_WARMUP_THREADS):
        remaining = None if deadline is None else max(0.0, deadline - time.perf_counter())
        thread.join(remaining)
    with _LOCK:
        return DEFAULT_WARMUP_LANGUAGES in _ENGINES


def shutdown_ocr_warmup(timeout: float = 1.0) -> None:
    deadline = time.perf_counter() + timeout
    for thread in list(_WARMUP_THREADS):
        if not thread.is_alive():
            continue
        thread.join(max(0.0, deadline - time.perf_counter()))
    with _LOCK:
        _ENGINES.clear()
        _WARMUP_THREADS[:] = [thread for thread in _WARMUP_THREADS if thread.is_alive()]
    PaddleOCREngine.clear_shared_engines()


def _warm_ocr_engine(key: tuple[str, ...]) -> None:
    started = time.perf_counter()
    try:
        engine = PaddleOCREngine(list(key))
        with tempfile.TemporaryDirectory(prefix="hydra-ocr-warmup-") as folder:
            sample = Path(folder) / "warmup.png"
            _write_warmup_image(sample)
            engine.analyze(sample, key[0] if key else None)
        with _LOCK:
            _ENGINES[key] = engine
        LOGGER.info("OCR warmup finished for %s in %.2fs", ",".join(key), time.perf_counter() - started)
    except Exception:
        LOGGER.exception("OCR warmup failed for %s", ",".join(key))


def _write_warmup_image(path: Path) -> None:
    image = Image.new("RGB", (320, 140), "white")
    draw = ImageDraw.Draw(image)
    font = _warmup_font(42)
    draw.text((32, 42), "テスト", fill="black", font=font)
    image.save(path)


def _warmup_font(size: int):
    for family in ("Yu Gothic", "MS Gothic", "Meiryo"):
        candidate = find_font_file(family)
        if candidate is not None:
            try:
                return ImageFont.truetype(str(candidate), size)
            except OSError:
                pass
    return ImageFont.load_default()
