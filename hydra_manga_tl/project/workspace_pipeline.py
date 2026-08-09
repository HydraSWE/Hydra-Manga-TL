"""Pipeline and export orchestration for workspace projects."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable

from hydra_manga_tl.core.state import APP_STATE
from hydra_manga_tl.core.user_errors import pipeline_error, render_error
from hydra_manga_tl.phase.pipeline import _completed_project_output
from hydra_manga_tl.project.export import export_archive, export_images


class WorkspacePipelineMixin:
    """Provide pipeline lifecycle callbacks and export metadata behavior."""
    def start_pipeline(self, image_ids: set[str] | None = None, *, retranslate: bool = False) -> bool:
        if self.current is None:
            return False
        eligible = {"pending", "queued", "failed", "cancelled"}
        if retranslate and image_ids:
            for image in self.current.images:
                if image.id in image_ids:
                    image.status = "queued"
                    image.error = ""
            self.save()
        if retranslate and image_ids:
            self._active_job_ids = [
                image.id for image in self.current.images
                if image.id in image_ids
            ]
        else:
            self._active_job_ids = [
                image.id for image in self.current.images
                if image.status in eligible and (image_ids is None or image.id in image_ids)
            ]
        if not self._active_job_ids:
            return False
        self._active_job_completed = 0
        if hasattr(self.pipeline, "set_request_type"):
            self.pipeline.set_request_type("batch" if image_ids is None else "selected")
        if hasattr(self.pipeline, "set_force_retranslate"):
            self.pipeline.set_force_retranslate(retranslate)
        started = self.pipeline.process_project(self.current, set(self._active_job_ids))
        if started:
            APP_STATE.set_busy(True)
            APP_STATE.set_pipeline("analyzing", 0, len(self._active_job_ids), "Preparing translation models…")
        else:
            self._active_job_ids = []
        return started

    def cancel_pipeline(self) -> None:
        self.cancel_active_requests()

    def cancel_active_requests(self) -> bool:
        """Cancel queued/current translation work and queued manual renders."""
        requested = False
        if self.pipeline.running:
            self.pipeline.cancel()
            requested = True
            APP_STATE.set_pipeline(
                "cancelled",
                APP_STATE.progress_current,
                APP_STATE.progress_total,
                "Cancelling after current stage...",
            )
        requested = self.manual_service.cancel_all() or requested
        for request_id in tuple(self._manual_render_contexts):
            requested = self.render_queue.cancel(request_id) or requested
        return requested

    def export(
        self,
        destination: Path,
        *,
        mode: str = "translated",
        image_format: str = "png",
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> int:
        if self.current is None:
            return 0
        count = export_images(
            self.current,
            destination,
            mode=mode,
            image_format=image_format,
            progress_callback=progress_callback,
        )
        APP_STATE.set_export(str(destination.resolve()), count)
        self.record_export(
            export_type="folder",
            path=destination,
            count=count,
            mode=mode,
            image_format=image_format,
        )
        return count

    def export_archive(
        self,
        destination: Path,
        *,
        mode: str = "translated",
        image_format: str = "png",
        archive_format: str = "zip",
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path | None:
        if self.current is None:
            return None
        archive = export_archive(
            self.current,
            destination,
            mode=mode,
            image_format=image_format,
            archive_format=archive_format,
            progress_callback=progress_callback,
        )
        APP_STATE.set_export(str(archive.resolve()), 1)
        self.record_export(
            export_type="archive",
            path=archive,
            count=len(self.current.images),
            mode=mode,
            image_format=archive_format,
        )
        return archive

    def record_export(
        self,
        *,
        export_type: str,
        path: Path,
        count: int,
        mode: str,
        image_format: str,
    ) -> None:
        """Persist last-export metadata to the open project file.

        Must be called only after a successful export. No-op if no project is open.
        """
        if self.current is None:
            return
        self.current.last_exported_at = datetime.now(timezone.utc).isoformat()
        self.current.last_export_path = str(path.resolve())
        self.current.last_export_type = export_type
        self.current.last_export_count = count
        self.current.last_export_mode = mode
        self.current.last_export_format = image_format
        self.save()

    def _find_image(self, image_id: str):
        if self.current is None:
            return -1, None
        for index, image in enumerate(self.current.images):
            if image.id == image_id:
                return index, image
        return -1, None

    def _on_progress(self, image_id: str, stage: str, current: int, total: int, message: str) -> None:
        index, image = self._find_image(image_id)
        if image is not None:
            image.status = stage
            self.image_updated.emit(index)
        APP_STATE.set_pipeline(stage, current, total, message)
        self.save()

    def _on_image_finished(self, image_id: str, result: dict) -> None:
        index, image = self._find_image(image_id)
        if image is None:
            return
        for key, value in result.items():
            setattr(image, key, value)
        self._update_image_review_status(index)
        self.save()
        if image.manual_regions or image.edits or image.suppressed_auto_group_indices:
            try:
                self.rerender_image(index)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                image.status = "review"
                image.error = render_error(error)
                self.save()
        self.image_updated.emit(index)
        if image_id in self._active_job_ids:
            self._active_job_completed += 1
            APP_STATE.set_pipeline(
                "complete", self._active_job_completed, len(self._active_job_ids),
                f"Completed {Path(image.source_path).name}",
            )

    def _on_image_failed(self, image_id: str, message: str) -> None:
        index, image = self._find_image(image_id)
        user_message = pipeline_error(message)
        if image is not None:
            image.status = "failed"
            image.error = user_message
            self.image_updated.emit(index)
        APP_STATE.report_error(user_message)
        self.save()
        if image_id in self._active_job_ids:
            self._active_job_completed += 1
            failed_name = Path(image.source_path).name if image is not None else "pipeline initialization"
            APP_STATE.set_pipeline(
                "failed", self._active_job_completed, len(self._active_job_ids),
                f"Failed: {failed_name}",
            )

    def _on_completed(self, cancelled: bool) -> None:
        if cancelled and self.current is not None:
            cancellable = {
                "preprocessing",
                "OCR",
                "ocr",
                "translating",
                "localizing",
                "rendering",
                "reconstructing",
                "analyzing",
            }
            for image in self.current.images:
                if image.id not in self._active_job_ids:
                    continue
                completed = _completed_project_output(self.current, image)
                if completed is not None:
                    for key, value in completed.items():
                        setattr(image, key, value)
                    continue
                if image.status in cancellable:
                    image.status = "cancelled"
        APP_STATE.set_busy(False)
        total = len(self._active_job_ids)
        completed = self._active_job_completed if cancelled else total
        APP_STATE.set_pipeline(
            "cancelled" if cancelled else "ready", completed, total,
            "Cancelled" if cancelled else "Translation complete",
        )
        self.save()
        APP_STATE.refresh_project()
        self.pipeline_finished.emit(cancelled)
        self._active_job_ids = []


