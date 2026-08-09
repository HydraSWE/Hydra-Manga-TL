"""Execution entry points for PipelineService."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt

from hydra_manga_tl.core.settings import SETTINGS
from hydra_manga_tl.phase.pipeline_config import project_pipeline_config
from hydra_manga_tl.translation.queue import CancellationToken, RequestCancelled
from hydra_manga_tl.translation.requests import (
    TranslationRequest,
    TranslationRequestStatus,
    TranslationRequestType,
)


class PipelineServiceExecutionMixin:
    def process_project(self, project, image_ids: set[str] | None = None) -> bool:
        if self.running:
            self._next_force_retranslate = False
            self._next_request_type = None
            return False
        settings = self._settings or SETTINGS
        force_retranslate = self._next_force_retranslate
        self._next_force_retranslate = False
        request_type = self._next_request_type or (
            TranslationRequestType.SELECTED
            if image_ids is not None
            else TranslationRequestType.BATCH
        )
        self._next_request_type = None
        prefix = request_type.value
        config = project_pipeline_config(project, settings)
        # The fallback is an explicit user selection. Keep local providers
        # lazy, but allow Marian to be constructed if a cloud engine fails.
        config["allow_local_fallback_for_cloud"] = True
        config["force_retranslate"] = force_retranslate
        items = self._planned_project_items(
            project,
            image_ids,
            config,
            force_retranslate=force_retranslate,
        )
        if not items:
            return False
        self._request_prefix_by_image = {
            str(item["id"]): prefix for item in items
        }
        self._active_request_images = set(self._request_prefix_by_image)
        for item in items:
            self.request_state_changed.emit(
                self._request_id(str(item["id"])), "queued", "Queued",
            )
        image_positions = {
            image.id: index for index, image in enumerate(project.images)
        }
        requests = tuple(
            TranslationRequest(
                request_id=self._request_id(str(item["id"])),
                type=request_type,
                project_id=project.id,
                image_id=str(item["id"]),
                image_index=image_positions[str(item["id"])],
                source_path=Path(item["source_path"]),
                target_language=project.target_language,
                source_language=project.source_language,
                metadata={"item": dict(item)},
            )
            for item in items
        )
        self._future = self._queue.submit_group(
            requests,
            lambda grouped, token, group_progress: self._run_request_group(
                grouped,
                token,
                group_progress,
                items=items,
                artifacts=project.artifacts,
                target=project.target_language,
                config=config,
            ),
        )
        return True

    def cancel(self) -> None:
        if not self._active_request_images:
            return
        image_id = next(iter(self._active_request_images))
        self._queue.cancel(self._request_id(image_id))

    def _run_request_group(
        self,
        requests: tuple[TranslationRequest, ...],
        token: CancellationToken,
        group_progress,
        *,
        items: list[dict],
        artifacts: Path,
        target: str,
        config: dict[str, Any],
    ) -> dict[str, None]:
        request_id_by_image = {
            request.image_id: request.request_id for request in requests
        }
        pipeline_facade = import_module("hydra_manga_tl.phase.pipeline")
        worker = pipeline_facade.PipelineWorker(
            items,
            artifacts,
            target,
            token.event,
            config,
        )
        self._worker = worker
        cancelled: list[bool] = []

        def safe_group_progress(
            request_id: str,
            status: TranslationRequestStatus,
            message: str = "",
        ) -> None:
            try:
                group_progress(request_id, status, message)
            except RequestCancelled:
                if not token.requested:
                    raise

        def queue_stage(
            image_id: str,
            stage: str,
            position: int,
            total: int,
            message: str,
        ) -> None:
            safe_group_progress(
                request_id_by_image[image_id],
                self._task_status(stage),
                message,
            )

        def queue_finished(image_id: str, result: dict) -> None:
            safe_group_progress(
                request_id_by_image[image_id],
                TranslationRequestStatus.DONE,
                "Done",
            )

        def queue_failed(image_id: str, message: str) -> None:
            target_ids = (
                (request_id_by_image[image_id],)
                if image_id in request_id_by_image
                else tuple(request_id_by_image.values())
            )
            for request_id in target_ids:
                safe_group_progress(
                    request_id,
                    TranslationRequestStatus.FAILED,
                    message,
                )

        worker.stage.connect(queue_stage, Qt.ConnectionType.DirectConnection)
        worker.stage.connect(self.progress)
        worker.stage.connect(self._on_worker_stage)
        if hasattr(worker, "scheduler_snapshot"):
            worker.scheduler_snapshot.connect(self.scheduler_stats)
        worker.image_finished.connect(queue_finished, Qt.ConnectionType.DirectConnection)
        worker.image_finished.connect(self.image_finished)
        worker.image_finished.connect(self._on_worker_image_finished)
        worker.image_failed.connect(queue_failed, Qt.ConnectionType.DirectConnection)
        worker.image_failed.connect(self.image_failed)
        worker.image_failed.connect(self._on_worker_image_failed)
        worker.finished.connect(cancelled.append, Qt.ConnectionType.DirectConnection)
        worker.finished.connect(self._finish)
        worker.run()
        if cancelled and cancelled[-1]:
            token.cancel()
            token.raise_if_cancelled()
        return {request.request_id: None for request in requests}
