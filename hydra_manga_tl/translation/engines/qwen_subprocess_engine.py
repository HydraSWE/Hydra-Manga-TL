"""Process-isolated wrapper for the native Qwen GGUF translation engine."""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import threading
import time
import uuid
from multiprocessing.connection import Connection
from typing import Any

from hydra_manga_tl.translation.engines.base import PageDialogue, PageTranslation, prepare_dialogue_item
from hydra_manga_tl.translation.qwen_worker import run_qwen_worker


LOGGER = logging.getLogger(__name__)
DEFAULT_QWEN_STARTUP_TIMEOUT = 420.0
DEFAULT_QWEN_REQUEST_TIMEOUT = 300.0


def _coerce_timeout(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return max(1.0, float(value))
    except (TypeError, ValueError):
        return default


class QwenWorkerCrashed(RuntimeError):
    """Raised when the Qwen child process exits or stops responding."""


class QwenSubprocessEngine:
    """TranslationEngine wrapper that keeps llama-cpp native faults out of Qt."""

    def __init__(
        self,
        *,
        model_path: str | None = None,
        glossary: dict[str, str] | None = None,
        model_name: str = "Qwen3-4B-Instruct-2507",
        runtime_config: dict[str, Any] | None = None,
        startup_timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> None:
        self.model_path = model_path
        self.glossary = glossary or {}
        self.model_name = model_name
        self.runtime_config = runtime_config or {}
        self.startup_timeout = _coerce_timeout(
            startup_timeout or os.environ.get("QWEN_WORKER_STARTUP_TIMEOUT"),
            DEFAULT_QWEN_STARTUP_TIMEOUT,
        )
        self.request_timeout = _coerce_timeout(
            request_timeout or os.environ.get("QWEN_WORKER_REQUEST_TIMEOUT"),
            DEFAULT_QWEN_REQUEST_TIMEOUT,
        )
        self._lock = threading.RLock()
        self._process: mp.Process | None = None
        self._connection: Connection | None = None
        self._ready = False

    @property
    def engine_id(self) -> str:
        return f"qwen-gguf:{self.model_name}"

    def load(self) -> None:
        with self._lock:
            if self._ready and self._process is not None and self._process.is_alive():
                return
            self._start_worker()
            self._wait_until_ready(self.startup_timeout)

    def translate_page(self, page: PageDialogue) -> PageTranslation:
        with self._lock:
            self.load()
            connection = self._require_connection()
            request_id = str(uuid.uuid4())
            connection.send({
                "command": "translate_page",
                "request_id": request_id,
                "page": {
                    "source_language": page.source_language,
                    "target_language": page.target_language,
                    "dialogue": [prepare_dialogue_item(item) for item in page.dialogue],
                    "page_context": page.page_context,
                },
            })
            response = self._wait_for_response(request_id, self.request_timeout)
            if not response.get("ok"):
                raise RuntimeError(self._format_worker_error(response))
            payload = dict(response.get("translation") or {})
            return PageTranslation(
                source_language=str(payload.get("source_language") or page.source_language),
                target_language=str(payload.get("target_language") or page.target_language),
                translations=list(payload.get("translations") or []),
            )

    def unload(self) -> None:
        with self._lock:
            connection = self._connection
            process = self._process
            if connection is not None and process is not None and process.is_alive():
                try:
                    connection.send({"command": "shutdown", "request_id": str(uuid.uuid4())})
                except (BrokenPipeError, EOFError, OSError):
                    pass
            self._close_worker(force=False)

    def cancel(self) -> None:
        self._close_worker(force=True)

    def _start_worker(self) -> None:
        self._close_worker(force=True)
        parent_connection, child_connection = mp.get_context("spawn").Pipe(duplex=True)
        process = mp.get_context("spawn").Process(
            target=run_qwen_worker,
            args=(
                child_connection,
                {
                    "model_path": self.model_path,
                    "glossary": self.glossary,
                    "model_name": self.model_name,
                    "runtime_config": self.runtime_config,
                },
            ),
            name="HydraQwenWorker",
            daemon=True,
        )
        process.start()
        child_connection.close()
        self._process = process
        self._connection = parent_connection
        self._ready = False
        LOGGER.info("Qwen worker started pid=%s model=%s", process.pid, self.model_name)

    def _require_connection(self) -> Connection:
        if self._connection is None:
            raise QwenWorkerCrashed("Qwen worker is not connected")
        return self._connection

    def _wait_until_ready(self, timeout: float) -> None:
        deadline = time.perf_counter() + timeout
        connection = self._require_connection()
        while time.perf_counter() < deadline:
            self._raise_if_worker_dead()
            if connection.poll(0.1):
                message = self._recv_message(connection)
                event = str(message.get("event") or "")
                state = str(message.get("state") or "")
                if event == "state" and state == "READY":
                    self._ready = True
                    LOGGER.info("Qwen worker ready pid=%s engine=%s", self._pid(), message.get("engine_id", ""))
                    return
                if event == "error":
                    self._close_worker(force=True)
                    raise RuntimeError(self._format_worker_error(message))
            time.sleep(0.02)
        pid = self._pid()
        self._close_worker(force=True)
        raise TimeoutError(f"Qwen worker warm-up timed out after {timeout:.1f}s; pid={pid}")

    def _wait_for_response(self, request_id: str, timeout: float) -> dict[str, Any]:
        deadline = time.perf_counter() + timeout
        connection = self._require_connection()
        while time.perf_counter() < deadline:
            self._raise_if_worker_dead()
            if connection.poll(0.1):
                message = self._recv_message(connection)
                event = str(message.get("event") or "")
                if event == "error":
                    self._close_worker(force=True)
                    raise RuntimeError(self._format_worker_error(message))
                if event == "response" and str(message.get("request_id") or "") == request_id:
                    return dict(message)
            time.sleep(0.02)
        pid = self._pid()
        self._close_worker(force=True)
        raise TimeoutError(f"Qwen worker request timed out after {timeout:.1f}s; pid={pid}")

    def _raise_if_worker_dead(self) -> None:
        process = self._process
        if process is None:
            raise QwenWorkerCrashed("Qwen worker is not running")
        if not process.is_alive():
            exitcode = process.exitcode
            self._close_worker(force=True)
            raise QwenWorkerCrashed(f"Qwen worker exited unexpectedly; pid={process.pid}; exitcode={exitcode}")

    def _recv_message(self, connection: Connection) -> dict[str, Any]:
        try:
            message = connection.recv()
        except (BrokenPipeError, EOFError, OSError) as error:
            pid = self._pid()
            self._close_worker(force=True)
            raise QwenWorkerCrashed(f"Qwen worker pipe closed unexpectedly; pid={pid}") from error
        if not isinstance(message, dict):
            raise RuntimeError(f"Qwen worker returned invalid message: {type(message).__name__}")
        return message

    def _pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    def _close_worker(self, *, force: bool) -> None:
        connection, self._connection = self._connection, None
        process, self._process = self._process, None
        self._ready = False
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if process is None:
            return
        process.join(1.0)
        if process.is_alive():
            process.terminate()
            process.join(2.0)
        if force and process.is_alive():
            process.kill()
            process.join(2.0)
        if process.exitcode is not None:
            LOGGER.info("Qwen worker stopped pid=%s exitcode=%s", process.pid, process.exitcode)

    @staticmethod
    def _format_worker_error(message: dict[str, Any]) -> str:
        payload = dict(message.get("error") or {})
        error_type = str(payload.get("type") or "RuntimeError")
        error_message = str(payload.get("message") or "Qwen worker failed")
        return f"{error_type}: {error_message}"
