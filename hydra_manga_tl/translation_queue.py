"""Request-scoped translation queue with cooperative cancellation."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from .translation_requests import TranslationRequest, TranslationRequestStatus


class CancellationToken:
    def __init__(self) -> None:
        self._requested = threading.Event()

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    @property
    def event(self) -> threading.Event:
        return self._requested

    def cancel(self) -> None:
        self._requested.set()

    def raise_if_cancelled(self) -> None:
        if self.requested:
            raise RequestCancelled("Request cancelled")


class RequestCancelled(RuntimeError):
    pass


ProgressCallback = Callable[[TranslationRequestStatus, str], None]
RequestHandler = Callable[[TranslationRequest, CancellationToken, ProgressCallback], Any]
GroupProgressCallback = Callable[[str, TranslationRequestStatus, str], None]
RequestGroupHandler = Callable[
    [tuple[TranslationRequest, ...], CancellationToken, GroupProgressCallback],
    Any,
]


class TranslationQueue(QObject):
    """Serialize translation requests while exposing stable task-state events."""

    state_changed = Signal(str, str, str)
    completed = Signal(str, object)
    failed = Signal(str, object)
    busy_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="HydraTranslationQueue")
        self._lock = threading.RLock()
        self._tokens: dict[str, CancellationToken] = {}
        self._futures: dict[str, Future] = {}
        self._request_groups: dict[str, tuple[str, ...]] = {}
        self._states: dict[str, TranslationRequestStatus] = {}
        self._closed = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._futures)

    def status(self, request_id: str) -> TranslationRequestStatus | None:
        with self._lock:
            return self._states.get(request_id)

    def submit(self, request: TranslationRequest, handler: RequestHandler) -> Future:
        with self._lock:
            if self._closed:
                raise RuntimeError("Translation queue is shut down")
            if request.request_id in self._futures:
                raise ValueError(f"Duplicate request id: {request.request_id}")
            was_idle = not self._futures
            token = CancellationToken()
            start_gate = threading.Event()
            self._tokens[request.request_id] = token
            self._request_groups[request.request_id] = (request.request_id,)
            self._states[request.request_id] = TranslationRequestStatus.QUEUED
            future = self._executor.submit(
                self._execute_after_gate,
                start_gate,
                request,
                token,
                handler,
            )
            self._futures[request.request_id] = future
        self.state_changed.emit(request.request_id, TranslationRequestStatus.QUEUED.value, "Queued")
        if was_idle:
            self.busy_changed.emit(True)
        future.add_done_callback(lambda completed: self._finish(request, completed))
        start_gate.set()
        return future

    def submit_group(
        self,
        requests: tuple[TranslationRequest, ...] | list[TranslationRequest],
        handler: RequestGroupHandler,
    ) -> Future:
        grouped = tuple(requests)
        if not grouped:
            raise ValueError("Translation request groups cannot be empty")
        request_ids = tuple(request.request_id for request in grouped)
        if len(set(request_ids)) != len(request_ids):
            raise ValueError("Translation request group contains duplicate request ids")
        with self._lock:
            if self._closed:
                raise RuntimeError("Translation queue is shut down")
            duplicates = [request_id for request_id in request_ids if request_id in self._futures]
            if duplicates:
                raise ValueError(f"Duplicate request id: {duplicates[0]}")
            was_idle = not self._futures
            token = CancellationToken()
            start_gate = threading.Event()
            future = self._executor.submit(
                self._execute_group_after_gate,
                start_gate,
                grouped,
                token,
                handler,
            )
            for request_id in request_ids:
                self._tokens[request_id] = token
                self._futures[request_id] = future
                self._request_groups[request_id] = request_ids
                self._states[request_id] = TranslationRequestStatus.QUEUED
        for request_id in request_ids:
            self.state_changed.emit(
                request_id, TranslationRequestStatus.QUEUED.value, "Queued",
            )
        if was_idle:
            self.busy_changed.emit(True)
        future.add_done_callback(
            lambda completed: self._finish_group(grouped, completed),
        )
        start_gate.set()
        return future

    def _execute_after_gate(
        self,
        start_gate: threading.Event,
        request: TranslationRequest,
        token: CancellationToken,
        handler: RequestHandler,
    ) -> Any:
        start_gate.wait()
        return self._execute(request, token, handler)

    def _execute_group_after_gate(
        self,
        start_gate: threading.Event,
        requests: tuple[TranslationRequest, ...],
        token: CancellationToken,
        handler: RequestGroupHandler,
    ) -> Any:
        start_gate.wait()
        return self._execute_group(requests, token, handler)

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            token = self._tokens.get(request_id)
            future = self._futures.get(request_id)
            if token is None or future is None:
                return False
            request_ids = self._request_groups.get(request_id, (request_id,))
            token.cancel()
            removed_while_queued = future.cancel()
        if removed_while_queued:
            for grouped_request_id in request_ids:
                self._set_state(
                    grouped_request_id,
                    TranslationRequestStatus.CANCELLED,
                    "Cancelled",
                )
        return True

    def _execute(
        self,
        request: TranslationRequest,
        token: CancellationToken,
        handler: RequestHandler,
    ) -> Any:
        token.raise_if_cancelled()

        def progress(status: TranslationRequestStatus, message: str = "") -> None:
            token.raise_if_cancelled()
            self._set_state(request.request_id, status, message)

        return handler(request, token, progress)

    def _execute_group(
        self,
        requests: tuple[TranslationRequest, ...],
        token: CancellationToken,
        handler: RequestGroupHandler,
    ) -> Any:
        token.raise_if_cancelled()
        request_ids = {request.request_id for request in requests}

        def progress(
            request_id: str,
            status: TranslationRequestStatus,
            message: str = "",
        ) -> None:
            token.raise_if_cancelled()
            if request_id not in request_ids:
                raise ValueError(f"Unknown grouped request id: {request_id}")
            self._set_state(request_id, status, message)

        return handler(requests, token, progress)

    def _finish(self, request: TranslationRequest, future: Future) -> None:
        try:
            result = future.result()
        except (RequestCancelled, BaseException) as error:
            token = self._tokens.get(request.request_id)
            if isinstance(error, RequestCancelled) or (token is not None and token.requested):
                self._set_state(request.request_id, TranslationRequestStatus.CANCELLED, "Cancelled")
            else:
                self._set_state(request.request_id, TranslationRequestStatus.FAILED, str(error))
                payload = {
                    "message": str(error) or type(error).__name__,
                    "exception_type": type(error).__name__,
                }
                manual_result = getattr(error, "result", None)
                if isinstance(manual_result, dict):
                    payload["manual_result"] = manual_result
                self.failed.emit(request.request_id, payload)
        else:
            self._set_state(request.request_id, TranslationRequestStatus.DONE, "Done")
            self.completed.emit(request.request_id, result)
        finally:
            with self._lock:
                self._tokens.pop(request.request_id, None)
                self._futures.pop(request.request_id, None)
                self._request_groups.pop(request.request_id, None)
                now_idle = not self._futures
            if now_idle:
                self.busy_changed.emit(False)

    def _finish_group(
        self,
        requests: tuple[TranslationRequest, ...],
        future: Future,
    ) -> None:
        request_ids = tuple(request.request_id for request in requests)
        token = self._tokens.get(request_ids[0])
        try:
            result = future.result()
        except BaseException as error:
            cancelled = isinstance(error, RequestCancelled) or (
                token is not None and token.requested
            )
            status = (
                TranslationRequestStatus.CANCELLED
                if cancelled
                else TranslationRequestStatus.FAILED
            )
            message = "Cancelled" if cancelled else str(error)
            for request_id in request_ids:
                self._set_state(request_id, status, message)
                if not cancelled:
                    self.failed.emit(request_id, {
                        "message": str(error) or type(error).__name__,
                        "exception_type": type(error).__name__,
                    })
        else:
            for request_id in request_ids:
                current = self.status(request_id)
                if current in {
                    TranslationRequestStatus.FAILED,
                    TranslationRequestStatus.CANCELLED,
                }:
                    continue
                if current is not TranslationRequestStatus.DONE:
                    self._set_state(
                        request_id, TranslationRequestStatus.DONE, "Done",
                    )
                item_result = (
                    result.get(request_id)
                    if isinstance(result, dict) and request_id in result
                    else result
                )
                self.completed.emit(request_id, item_result)
        finally:
            with self._lock:
                for request_id in request_ids:
                    self._tokens.pop(request_id, None)
                    self._futures.pop(request_id, None)
                    self._request_groups.pop(request_id, None)
                now_idle = not self._futures
            if now_idle:
                self.busy_changed.emit(False)

    def _set_state(
        self,
        request_id: str,
        status: TranslationRequestStatus,
        message: str,
    ) -> None:
        with self._lock:
            self._states[request_id] = status
        self.state_changed.emit(request_id, status.value, message)

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            tokens = tuple(self._tokens.values())
        for token in tokens:
            token.cancel()
        self._executor.shutdown(wait=wait, cancel_futures=True)


TRANSLATION_QUEUE = TranslationQueue()


def shutdown_translation_queue() -> None:
    TRANSLATION_QUEUE.shutdown()
