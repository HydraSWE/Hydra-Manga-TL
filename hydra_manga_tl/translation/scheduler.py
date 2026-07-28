"""Bounded, ordered page translation scheduling for Fast mode."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
import os
from pathlib import Path
import threading
import time
from typing import Callable, Protocol

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


class PageTranslationStage(Protocol):
    """Provider-neutral hook for future page translation stages."""

    def __call__(self, job: ParallelPageJob) -> PageTranslationOutcome: ...


def auto_worker_count(logical_threads: int | None = None) -> int:
    threads = max(1, int(logical_threads or os.cpu_count() or 1))
    if threads <= 4:
        return 2
    if threads <= 8:
        return 4
    return 6


def resolve_worker_count(override: int, page_count: int) -> int:
    requested = auto_worker_count() if int(override or 0) == 0 else max(1, min(6, int(override)))
    return max(1, min(requested, max(1, int(page_count))))


class ParallelPageScheduler:
    """Dynamically schedule translation and release results in page order.

    The combined active/reorder window is bounded to ``workers * 2``. Jobs
    outside that window remain lightweight immutable descriptors.
    """

    def __init__(
        self,
        workers: int,
        *,
        gpu_state: str,
        cancel_event: threading.Event,
        snapshot_callback: Callable[[SchedulerSnapshot], None] | None = None,
    ) -> None:
        self.workers = max(1, min(6, int(workers)))
        self.window = self.workers * 2
        self.gpu_state = str(gpu_state)
        self.cancel_event = cancel_event
        self.snapshot_callback = snapshot_callback

    def run(
        self,
        jobs: list[ParallelPageJob],
        stage: PageTranslationStage,
        commit: Callable[[PageTranslationOutcome], PageTranslationOutcome | None],
    ) -> list[PageTranslationOutcome]:
        ordered = sorted(jobs, key=lambda item: item.page_index)
        if not ordered:
            return []
        total = len(ordered)
        completed = failed = 0
        next_submit = 0
        next_commit = 0
        active: dict[Future[PageTranslationOutcome], ParallelPageJob] = {}
        buffered: dict[int, PageTranslationOutcome] = {}
        committed: list[PageTranslationOutcome] = []

        def publish() -> None:
            if self.snapshot_callback is not None:
                gpu_state = (
                    "Active"
                    if self.gpu_state == "Idle" and active
                    else self.gpu_state
                )
                self.snapshot_callback(SchedulerSnapshot(
                    configured_workers=self.workers,
                    active_workers=len(active),
                    queued=max(0, total - next_submit),
                    completed=completed,
                    failed=failed,
                    total=total,
                    gpu_state=gpu_state,
                ))

        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="HydraFastPage") as executor:
            while next_commit < total:
                while (
                    not self.cancel_event.is_set()
                    and next_submit < total
                    and len(active) < self.workers
                    and len(active) + len(buffered) < self.window
                ):
                    job = ordered[next_submit]
                    active[executor.submit(stage, job)] = job
                    next_submit += 1
                    publish()

                expected = ordered[next_commit]
                outcome = buffered.pop(expected.page_index, None)
                if outcome is not None:
                    committed_outcome = commit(outcome) or outcome
                    committed.append(committed_outcome)
                    completed += int(committed_outcome.succeeded)
                    failed += int(not committed_outcome.succeeded and not committed_outcome.cancelled)
                    next_commit += 1
                    publish()
                    continue

                if not active:
                    break

                done, _ = wait(tuple(active), return_when=FIRST_COMPLETED)
                for future in done:
                    job = active.pop(future)
                    try:
                        result = future.result()
                    except BaseException as error:
                        result = PageTranslationOutcome(
                            page_index=job.page_index,
                            image_id=job.image_id,
                            request_id=job.request_id,
                            attempts=1,
                            error=f"{type(error).__name__}: {error}",
                        )
                    buffered[result.page_index] = result
                publish()

            # Running calls are allowed to settle, but cancellation only commits
            # the already contiguous prefix. Executor shutdown waits cleanly.
            if self.cancel_event.is_set():
                for future in tuple(active):
                    future.cancel()

        publish()
        return committed


def timed_stage(
    job: ParallelPageJob,
    translate: Callable[[PageDialogue], tuple[PageTranslation, str, int]],
) -> PageTranslationOutcome:
    started = time.perf_counter()
    try:
        if job.page is None:
            raise ValueError("Translation stage received an unhydrated page job")
        result, provider_id, attempts = translate(job.page)
    except BaseException as error:
        return PageTranslationOutcome(
            page_index=job.page_index,
            image_id=job.image_id,
            request_id=job.request_id,
            attempts=int(getattr(error, "attempts", 1) or 1),
            elapsed_seconds=time.perf_counter() - started,
            error=f"{type(error).__name__}: {error}",
        )
    return PageTranslationOutcome(
        page_index=job.page_index,
        image_id=job.image_id,
        request_id=job.request_id,
        translation=result,
        provider_id=provider_id,
        attempts=attempts,
        elapsed_seconds=time.perf_counter() - started,
    )
