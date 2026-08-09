"""Background export worker for the workspace UI."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal

from hydra_manga_tl.core.state import APP_STATE
from hydra_manga_tl.core.user_errors import export_error
from hydra_manga_tl.project.workspace import WORKSPACE


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
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.output_type = output_type
        self.destination = destination
        self.image_format = image_format
        self.archive_format = archive_format

    def run(self) -> None:
        try:
            def report_progress(current: int, total: int) -> None:
                self.progress.emit(current, total)

            if self.output_type == "folder":
                res = WORKSPACE.export(
                    self.destination,
                    image_format=self.image_format,
                    progress_callback=report_progress,
                )
            elif self.output_type == "pdf":
                from hydra_manga_tl.project.export import export_pdf

                res = export_pdf(
                    WORKSPACE.current,
                    self.destination,
                    progress_callback=report_progress,
                )
                APP_STATE.set_export(str(res.resolve()), 1)
                WORKSPACE.record_export(
                    export_type="pdf",
                    path=res,
                    count=len(WORKSPACE.current.images) if WORKSPACE.current else 0,
                    mode="translated",
                    image_format="pdf",
                )
            else:
                res = WORKSPACE.export_archive(
                    self.destination,
                    image_format=self.image_format,
                    archive_format=self.archive_format,
                    progress_callback=report_progress,
                )
            self.finished.emit(self.output_type, res)
        except Exception as error:
            self.failed.emit(export_error(error))
