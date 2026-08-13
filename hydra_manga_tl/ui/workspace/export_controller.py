"""Export dialog and worker flow for the workspace screen."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from PySide6.QtCore import QThread, Slot
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from hydra_manga_tl.core.settings import SETTINGS
from hydra_manga_tl.core.state import APP_STATE
from hydra_manga_tl.project.workspace import WORKSPACE
from hydra_manga_tl.ui.dialogs import BackgroundWorkDialog, ExportOptionsDialog

from .export_worker import ExportWorker


class ExportControllerMixin:
    def _default_export_target(self) -> tuple[Path, str]:
        import re
        parent = Path(SETTINGS.export_root)
        name = "manga"
        if WORKSPACE.current:
            raw_name = WORKSPACE.current.name or "manga"
            name = re.sub(r"[^a-zA-Z0-9_\-]+", "_", raw_name).strip("_")
        return parent, name

    def _start_export_worker(self, output_type: str, destination: Path, *, image_format: str = "png", archive_format: str = "zip") -> None:
        self._export_destination = destination
        self._export_image_format = image_format
        self._export_archive_format = archive_format
        self._export_dialog = BackgroundWorkDialog(self)
        self._export_dialog.setWindowTitle("Exporting")
        self._export_dialog.message.setText(f"Exporting files to:\n{destination}\n\nPlease wait...")
        self._export_dialog.set_progress_visible(True)
        self._export_dialog.set_progress_fraction(0, 1)

        self._export_thread = QThread(self)
        self._export_worker = ExportWorker(
            output_type,
            destination,
            image_format=image_format,
            archive_format=archive_format,
            project=WORKSPACE.current,
        )
        self._export_worker.moveToThread(self._export_thread)

        self._export_thread.started.connect(self._export_worker.run)

        self._export_worker.progress.connect(self.export_progress_changed.emit)
        self._export_worker.finished.connect(self.export_finished.emit)
        self._export_worker.failed.connect(self.export_failed.emit)

        self._export_worker.finished.connect(self._export_thread.quit)
        self._export_worker.finished.connect(self._export_worker.deleteLater)
        self._export_worker.failed.connect(self._export_thread.quit)
        self._export_worker.failed.connect(self._export_worker.deleteLater)
        self._export_thread.finished.connect(self._export_thread.deleteLater)
        self._export_thread.finished.connect(self._clear_export_worker)

        self._export_thread.start()
        self._export_dialog.show()

    @Slot(int, int)
    def _on_export_progress(self, current: int, total: int) -> None:
        if hasattr(self, "_export_dialog") and self._export_dialog is not None:
            self._export_dialog.set_progress_fraction(current, total)

    @Slot(str, object)
    def _on_export_finished(self, ot: str, result):
        if hasattr(self, "_export_dialog") and self._export_dialog is not None:
            self._export_dialog.accept()
        self._record_successful_export(ot, result)
        from hydra_manga_tl.core.notifications import NOTIFICATION_SERVICE, NotificationEvent
        from pathlib import Path as _Path
        if ot == "folder":
            notif_msg = f"Exported {result} image(s)."
        else:
            notif_msg = f"Saved: {_Path(str(result)).name}"
        self._set_export_status(f"Export complete. {notif_msg}")
        NOTIFICATION_SERVICE.notify(
            NotificationEvent.EXPORT_COMPLETED,
            "Export complete",
            notif_msg,
        )

    @Slot(str)
    def _on_export_failed(self, err: str):
        if hasattr(self, "_export_dialog") and self._export_dialog is not None:
            self._export_dialog.reject()
        QMessageBox.warning(self, "Export failed", err)
        from hydra_manga_tl.core.notifications import NOTIFICATION_SERVICE, NotificationEvent
        NOTIFICATION_SERVICE.notify(
            NotificationEvent.EXPORT_FAILED,
            "Export failed",
            err[:120],
        )

    def _set_export_status(self, message: str) -> None:
        status = getattr(self, "status", None)
        if status is not None and hasattr(status, "setText"):
            status.setText(message)

    @Slot()
    def _clear_export_worker(self) -> None:
        self._export_thread = None
        self._export_worker = None
        self._export_dialog = None

    def _record_successful_export(self, output_type: str, result) -> None:
        if WORKSPACE.current is None:
            return
        if output_type == "folder":
            path = self._export_destination
            count = int(result)
            APP_STATE.set_export(str(path.resolve()), count)
            WORKSPACE.record_export(
                export_type="folder",
                path=path,
                count=count,
                mode="translated",
                image_format=self._export_image_format,
            )
            return
        path = Path(str(result))
        count = len(WORKSPACE.current.images)
        if output_type == "pdf":
            APP_STATE.set_export(str(path.resolve()), 1)
            WORKSPACE.record_export(
                export_type="pdf",
                path=path,
                count=count,
                mode="translated",
                image_format="pdf",
            )
            return
        APP_STATE.set_export(str(path.resolve()), 1)
        WORKSPACE.record_export(
            export_type="archive",
            path=path,
            count=count,
            mode="translated",
            image_format=self._export_archive_format,
        )

    def _export(self) -> None:
        workspace_package = sys.modules.get(__package__)
        dialog_type = getattr(workspace_package, "ExportOptionsDialog", ExportOptionsDialog)
        file_dialog = getattr(workspace_package, "QFileDialog", QFileDialog)
        dialog = dialog_type(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        output_type = str(dialog.output_type.currentData())
        image_format = str(dialog.image_format.currentData())

        export_root, base_name = self._default_export_target()

        if output_type == "folder":
            folder = file_dialog.getExistingDirectory(self, "Export image folder", str(export_root))
            if not folder:
                return
            self._start_export_worker(output_type, Path(folder) / base_name, image_format=image_format)
            return

        if output_type == "pdf":
            default_pdf_path = str(export_root / f"{base_name}.pdf")
            path, _ = file_dialog.getSaveFileName(self, "Export PDF", default_pdf_path, "PDF document (*.pdf);;All files (*.*)")
            if not path:
                return
            self._start_export_worker(output_type, Path(path))
            return

        archive_format = "cbz" if output_type == "cbz" else "zip"
        filter_label = "CBZ comic archive (*.cbz)" if archive_format == "cbz" else "ZIP archive (*.zip)"
        default_archive_path = str(export_root / f"{base_name}.{archive_format}")
        path, _ = file_dialog.getSaveFileName(self, "Export archive", default_archive_path, f"{filter_label};;All files (*.*)")
        if not path:
            return
        self._start_export_worker(output_type, Path(path), image_format=image_format, archive_format=archive_format)
