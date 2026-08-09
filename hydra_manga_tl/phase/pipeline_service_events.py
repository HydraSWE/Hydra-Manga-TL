"""Queue and worker event routing for PipelineService."""

from __future__ import annotations

from PySide6.QtCore import Slot

from hydra_manga_tl.translation.requests import TranslationRequestStatus


class PipelineServiceEventsMixin:
    @Slot(str, str, int, int, str)
    def _on_worker_stage(self, image_id: str, stage: str, position: int, total: int, message: str) -> None:
        normalized = self._task_status(stage).value
        self.request_state_changed.emit(self._request_id(image_id), normalized, message)

    @Slot(str, object)
    def _on_worker_image_finished(self, image_id: str, result: dict) -> None:
        self.request_state_changed.emit(self._request_id(image_id), "done", "Done")
        self._active_request_images.discard(image_id)

    @Slot(str, str)
    def _on_worker_image_failed(self, image_id: str, message: str) -> None:
        if image_id:
            self.request_state_changed.emit(self._request_id(image_id), "failed", message)
            self._active_request_images.discard(image_id)
            return
        for active_image_id in tuple(self._active_request_images):
            self.request_state_changed.emit(
                self._request_id(active_image_id), "failed", message,
            )
        self._active_request_images.clear()

    @Slot(str, object)
    def _on_queue_request_failed(self, request_id: str, result: dict) -> None:
        image_id = next(
            (
                image_id for image_id in self._active_request_images
                if self._request_id(image_id) == request_id
            ),
            None,
        )
        if image_id is None:
            return
        message = str(result.get("message") or "Pipeline request failed")
        self.image_failed.emit(image_id, message)
        self._on_worker_image_failed(image_id, message)
        if not self._active_request_images and self.running:
            self._finish(False)

    @Slot(str, str, str)
    def _on_queue_state_changed(
        self,
        request_id: str,
        status: str,
        message: str,
    ) -> None:
        if status != TranslationRequestStatus.CANCELLED.value or not self.running:
            return
        if not any(
            self._request_id(image_id) == request_id
            for image_id in self._active_request_images
        ):
            return
        self._finish(True)

    @Slot(bool)
    def _finish(self, cancelled: bool) -> None:
        if cancelled:
            for image_id in tuple(self._active_request_images):
                self.request_state_changed.emit(
                    self._request_id(image_id), "cancelled", "Cancelled",
                )
        self._active_request_images.clear()
        self._future = None
        self._worker = None
        self.completed.emit(cancelled)
        self._request_prefix_by_image.clear()
