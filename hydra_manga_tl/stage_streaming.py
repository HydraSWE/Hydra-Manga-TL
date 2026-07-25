"""Small bounded executor used by network-bound pipeline stages."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import threading
from typing import Callable, TypeVar


T = TypeVar("T")


class BoundedStageExecutor:
    def __init__(self, max_workers: int, *, queue_capacity: int | None = None, name: str = "HydraStage") -> None:
        workers = max(1, int(max_workers))
        capacity = max(workers, int(queue_capacity or workers * 2))
        self._slots = threading.BoundedSemaphore(capacity)
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)

    def submit(self, function: Callable[..., T], *args, **kwargs) -> Future[T]:
        self._slots.acquire()
        try:
            future = self._executor.submit(function, *args, **kwargs)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _: self._slots.release())
        return future

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
