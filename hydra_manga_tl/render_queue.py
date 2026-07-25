"""Application-level serialized render execution."""

from __future__ import annotations

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from .translation_requests import RenderRequest


RenderCallable = Callable[[Any, Any, str], Any]


class RenderQueue(QObject):
    """Run all render jobs one at a time and publish request-scoped results."""

    completed = Signal(str, object)
    failed = Signal(str, object)
    cancelled = Signal(str)
    busy_changed = Signal(bool)
    queued = Signal(str, int)

    def __init__(self) -> None:
        super().__init__()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="HydraRender")
        self._lock = threading.RLock()
        self._pending = 0
        self._futures: dict[str, Future] = {}
        self._closed = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._pending > 0

    @property
    def pending_count(self) -> int:
        with self._lock:
            return self._pending

    def submit(self, request: RenderRequest, renderer: RenderCallable) -> Future:
        with self._lock:
            if self._closed:
                raise RuntimeError("Render queue is shut down")
            was_idle = self._pending == 0
            self._pending += 1
            position = self._pending
        if was_idle:
            self.busy_changed.emit(True)
        self.queued.emit(request.request_id, position)
        future = self._executor.submit(self._execute, request, renderer)
        with self._lock:
            self._futures[request.request_id] = future
        future.add_done_callback(lambda completed: self._publish(request, completed))
        return future

    def cancel(self, request_id: str) -> bool:
        """Cancel a render that has not started; running renders finish safely."""
        with self._lock:
            future = self._futures.get(request_id)
        return bool(future is not None and future.cancel())

    @staticmethod
    def _execute(request: RenderRequest, renderer: RenderCallable) -> dict[str, Any]:
        output = renderer(request.result_path, request.render_dir, request.render_policy)
        return {
            "request_id": request.request_id,
            "render_dir": str(request.render_dir),
            "reason": request.reason,
            "output": output,
        }

    def _publish(self, request: RenderRequest, future: Future) -> None:
        try:
            result = future.result()
        except CancelledError:
            self.cancelled.emit(request.request_id)
        except BaseException as error:
            self.failed.emit(request.request_id, {
                "request_id": request.request_id,
                "reason": request.reason,
                "message": str(error) or type(error).__name__,
                "exception_type": type(error).__name__,
            })
        else:
            self.completed.emit(request.request_id, result)
        finally:
            with self._lock:
                self._futures.pop(request.request_id, None)
                self._pending = max(0, self._pending - 1)
                now_idle = self._pending == 0
            if now_idle:
                self.busy_changed.emit(False)

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=True)


RENDER_QUEUE = RenderQueue()


def shutdown_render_queue() -> None:
    RENDER_QUEUE.shutdown()
