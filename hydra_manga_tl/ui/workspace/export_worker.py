"""Background export worker for the workspace UI."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from hydra_manga_tl.core.user_errors import export_error
from hydra_manga_tl.project.export import export_archive, export_images, export_pdf


class ExportWorker(QObject):
    progress = Signal(int, int)
    finished = Signal(str, object)
    failed = Signal(str)

    def __init__(
        self,
        output_type: str,
        destination: Path,
        *,
        image_format: str = "png",
        archive_format: str = "zip",
        project=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.output_type = output_type
        self.destination = destination
        self.image_format = image_format
        self.archive_format = archive_format
        self.project = project

    def run(self) -> None:
        try:
            def report_progress(current: int, total: int) -> None:
                self.progress.emit(current, total)
                QThread.msleep(1)

            project = self.project
            if project is None:
                res = 0
                self.finished.emit(self.output_type, res)
                return

            if self.output_type == "folder":
                res = export_images(
                    project,
                    self.destination,
                    image_format=self.image_format,
                    progress_callback=report_progress,
                )
            elif self.output_type == "pdf":
                res = export_pdf(
                    project,
                    self.destination,
                    progress_callback=report_progress,
                )
            else:
                res = export_archive(
                    project,
                    self.destination,
                    image_format=self.image_format,
                    archive_format=self.archive_format,
                    progress_callback=report_progress,
                )
            self.finished.emit(self.output_type, res)
        except Exception as error:
            self.failed.emit(export_error(error))
