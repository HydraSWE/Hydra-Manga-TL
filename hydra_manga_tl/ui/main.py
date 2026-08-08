"""Top-level main window for Hydra Manga TL."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, QObject, Signal, Qt, Slot
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QStackedWidget, QProgressDialog

from hydra_manga_tl.project.import_scan import ImportScanResult, ImportScanWorker
from hydra_manga_tl.core.state import APP_STATE
from hydra_manga_tl.project.compatibility import (
    IncompatibleProjectError,
    InvalidProjectError,
    ProjectMigrationRequired,
)
from hydra_manga_tl.project.migrations.manager import migration_message
from hydra_manga_tl.core.user_errors import import_error, project_open_error
from hydra_manga_tl.ui.landing import ImportProgressScreen, LandingScreen
from hydra_manga_tl.ui.workspace import WorkspaceScreen
from hydra_manga_tl.project.workspace import WORKSPACE


class ProjectOpenWorker(QObject):
    finished = Signal(object)
    failed = Signal(Exception)

    def __init__(self, path: Path, *, allow_migration: bool = False):
        super().__init__()
        self.path = path
        self.allow_migration = allow_migration

    def run(self) -> None:
        try:
            project = WORKSPACE.load_project(
                self.path,
                allow_migration=self.allow_migration,
            )
            self.finished.emit(project)
        except Exception as error:
            self.failed.emit(error)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__(); self._close_after_pipeline = False; self.setWindowTitle("Hydra Manga TL"); self.setWindowIcon(QApplication.windowIcon()); self.resize(1500, 900); self.setMinimumSize(1050, 680)
        self._import_thread: QThread | None = None
        self._import_worker: ImportScanWorker | None = None
        self._import_paths: list[Path] = []
        self._open_thread: QThread | None = None
        self._open_worker: ProjectOpenWorker | None = None
        self.open_progress: QProgressDialog | None = None
        self.stack = QStackedWidget(); self.setCentralWidget(self.stack)
        self.landing = LandingScreen(); self.import_progress = ImportProgressScreen(); self.workspace = WorkspaceScreen()
        self.stack.addWidget(self.landing); self.stack.addWidget(self.import_progress); self.stack.addWidget(self.workspace)
        self.landing.inputs_selected.connect(self._open_inputs); self.landing.project_selected.connect(self.open_project)
        self.workspace.close_requested.connect(self.close_project); WORKSPACE.project_opened.connect(lambda _: self.stack.setCurrentWidget(self.workspace))
        APP_STATE.error_raised.connect(lambda message: self.statusBar().showMessage(message, 10000))
        WORKSPACE.pipeline_finished.connect(self._finish_pending_close)
        WORKSPACE.parallel_stats_changed.connect(self._show_parallel_status)
        self.stack.setCurrentWidget(self.landing)

    def _show_parallel_status(self, snapshot) -> None:
        project = WORKSPACE.current
        if project is None or project.quality != "Fast" or not APP_STATE.busy:
            return
        self.statusBar().showMessage(
            f"FAST MODE • Parallel • {snapshot.configured_workers} Workers • GPU {snapshot.gpu_state}"
        )

    def _open_inputs(self, paths: list[Path]) -> None:
        if self._import_thread is not None and self._import_thread.isRunning():
            return
        self._import_paths = paths
        self.import_progress.begin(paths); self.stack.setCurrentWidget(self.import_progress)
        self._import_thread = QThread(self); self._import_worker = ImportScanWorker(paths)
        self._import_worker.moveToThread(self._import_thread)
        self._import_thread.started.connect(self._import_worker.run)
        self._import_worker.progress.connect(self.import_progress.update_progress)
        self._import_worker.finished.connect(self._complete_import)
        self._import_worker.failed.connect(self._fail_import)
        self._import_worker.finished.connect(self._import_worker.deleteLater)
        self._import_worker.failed.connect(self._import_worker.deleteLater)
        self._import_worker.finished.connect(self._import_thread.quit)
        self._import_worker.failed.connect(self._import_thread.quit)
        self._import_thread.finished.connect(self._clean_import_worker)
        self._import_thread.start()

    def _complete_import(self, result: ImportScanResult) -> None:
        self.import_progress.show_result(result)
        try:
            name = WORKSPACE._default_project_name(self._import_paths)
            WORKSPACE.create_from_sources(result.sources, name)
            if result.unreadable:
                QMessageBox.warning(self, "Import Warning", f"Skipped {len(result.unreadable)} unreadable image(s).")
        except (OSError, ValueError) as error:
            self._fail_import(error)

    def _fail_import(self, error: BaseException | str) -> None:
        self.stack.setCurrentWidget(self.landing)
        QMessageBox.warning(self, "Cannot import images", import_error(error))

    def _clean_import_worker(self) -> None:
        if self._import_thread is not None:
            self._import_thread.deleteLater()
        self._import_worker = None; self._import_thread = None; self._import_paths = []

    def open_project(self, path: Path) -> None:
        try:
            project_file = path / "project.json" if path.is_dir() else path
            payload = json.loads(project_file.read_text(encoding="utf-8"))
            if "documents" in payload and "images" not in payload:
                WORKSPACE.open_project(path)
                return
            metadata = WORKSPACE.project_metadata(path)
            if metadata.migration_required:
                raise ProjectMigrationRequired(metadata)
            self._start_project_open(path)
        except ProjectMigrationRequired as required:
            prompt = QMessageBox(self)
            prompt.setIcon(QMessageBox.Icon.Question)
            prompt.setWindowTitle("Upgrade Hydra Project?")
            prompt.setText(migration_message(required.metadata))
            prompt.setStandardButtons(
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.Cancel
            )
            prompt.setDefaultButton(QMessageBox.StandardButton.Cancel)
            prompt.button(QMessageBox.StandardButton.Yes).setText("Upgrade")
            if prompt.exec() != QMessageBox.StandardButton.Yes:
                self.stack.setCurrentWidget(self.landing)
                return

            backup_path = path.with_name(f"{path.stem}_backup.json")
            if not backup_path.exists():
                shutil.copy2(path, backup_path)

            self._start_project_open(
                path,
                allow_migration=True,
                label="Upgrading Hydra Project...",
            )
        except IncompatibleProjectError as error:
            self.stack.setCurrentWidget(self.landing)
            QMessageBox.warning(self, "Project requires a newer Hydra", project_open_error(error))
        except (
            InvalidProjectError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            self.stack.setCurrentWidget(self.landing)
            QMessageBox.warning(self, "Cannot open project", project_open_error(error))

    def _start_project_open(
        self,
        path: Path,
        *,
        allow_migration: bool = False,
        label: str = "Opening Hydra Project...",
    ) -> None:
        if self._open_thread is not None and self._open_thread.isRunning():
            return
        APP_STATE.set_busy(True)
        self.open_progress = QProgressDialog(label, None, 0, 0, self)
        self.open_progress.setWindowTitle("Please Wait")
        self.open_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self.open_progress.setCancelButton(None)
        self.open_progress.show()

        self._open_thread = QThread(self)
        self._open_worker = ProjectOpenWorker(path, allow_migration=allow_migration)
        self._open_worker.moveToThread(self._open_thread)
        self._open_thread.started.connect(self._open_worker.run)
        self._open_worker.finished.connect(self._complete_project_open)
        self._open_worker.finished.connect(self._open_thread.quit)
        self._open_worker.finished.connect(self._open_worker.deleteLater)
        self._open_worker.failed.connect(self._fail_project_open)
        self._open_worker.failed.connect(self._open_thread.quit)
        self._open_worker.failed.connect(self._open_worker.deleteLater)
        self._open_thread.finished.connect(self._clean_project_open_worker)
        self._open_thread.start()

    @Slot(object)
    def _complete_project_open(self, project) -> None:
        try:
            WORKSPACE.activate_project(project)
            if self.open_progress is not None:
                self.open_progress.accept()
        except Exception as error:
            self._fail_project_open(error)
            return
        finally:
            APP_STATE.set_busy(False)

    @Slot(Exception)
    def _fail_project_open(self, error: Exception) -> None:
        if self.open_progress is not None:
            self.open_progress.reject()
        APP_STATE.set_busy(False)
        self.stack.setCurrentWidget(self.landing)
        QMessageBox.warning(self, "Cannot open project", project_open_error(error))

    @Slot()
    def _clean_project_open_worker(self) -> None:
        if self._open_thread is not None:
            self._open_thread.deleteLater()
        self._open_worker = None
        self._open_thread = None
        self.open_progress = None

    def open_startup_path(self, path: Path) -> None:
        if path.name == "project.json" or (path.is_dir() and (path / "project.json").is_file()): self.open_project(path)
        elif path.is_dir() and list(path.glob("*_translated_*.json")):
            try: WORKSPACE.import_phase2(path)
            except (OSError, ValueError, json.JSONDecodeError) as error: QMessageBox.warning(self, "Cannot import results", import_error(error))
        else: self._open_inputs([path])

    def _on_upgrade_failed(self, error: Exception) -> None:
        self._fail_project_open(error)

    def close_project(self) -> None:
        WORKSPACE.close(); self.landing.refresh_recent(); self.stack.setCurrentWidget(self.landing)

    def closeEvent(self, event) -> None:
        if not self._close_after_pipeline:
            reply = QMessageBox.question(
                self,
                "Exit Hydra Manga TL",
                "Do you really want to exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return

        if self._import_thread is not None and self._import_thread.isRunning():
            self._import_thread.requestInterruption(); self._import_thread.quit(); self._import_thread.wait(3000)
        if self._open_thread is not None and self._open_thread.isRunning():
            self._open_thread.requestInterruption(); self._open_thread.quit(); self._open_thread.wait(3000)
        self.workspace.stop_thumbnail_loading()
        if WORKSPACE.pipeline.running:
            self._close_after_pipeline = True; WORKSPACE.cancel_pipeline(); event.ignore(); self.statusBar().showMessage("Cancelling pipeline before closing...")
            return
        WORKSPACE.save(); super().closeEvent(event)

    def _finish_pending_close(self, cancelled: bool) -> None:
        if self._close_after_pipeline:
            self._close_after_pipeline = False; QTimer.singleShot(0, self.close)
