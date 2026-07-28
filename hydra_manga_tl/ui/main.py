"""Top-level main window for Hydra Manga TL."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QStackedWidget

from hydra_manga_tl.project.import_scan import ImportScanResult, ImportScanWorker
from hydra_manga_tl.core.state import APP_STATE
from hydra_manga_tl.ui.landing import ImportProgressScreen, LandingScreen
from hydra_manga_tl.ui.workspace import WorkspaceScreen
from hydra_manga_tl.project.workspace import WORKSPACE


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__(); self._close_after_pipeline = False; self.setWindowTitle("Hydra Manga TL"); self.setWindowIcon(QApplication.windowIcon()); self.resize(1500, 900); self.setMinimumSize(1050, 680)
        self._import_thread: QThread | None = None
        self._import_worker: ImportScanWorker | None = None
        self._import_paths: list[Path] = []
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
                self.statusBar().showMessage(f"Skipped {len(result.unreadable)} unreadable image(s)", 10000)
        except (OSError, ValueError) as error:
            self._fail_import(str(error))

    def _fail_import(self, message: str) -> None:
        self.stack.setCurrentWidget(self.landing)
        QMessageBox.warning(self, "Cannot import images", message)

    def _clean_import_worker(self) -> None:
        if self._import_thread is not None:
            self._import_thread.deleteLater()
        self._import_worker = None; self._import_thread = None; self._import_paths = []

    def open_project(self, path: Path) -> None:
        try: WORKSPACE.open_project(path)
        except (OSError, ValueError, json.JSONDecodeError) as error: QMessageBox.warning(self, "Cannot open project", str(error))

    def open_startup_path(self, path: Path) -> None:
        if path.name == "project.json" or (path.is_dir() and (path / "project.json").is_file()): self.open_project(path)
        elif path.is_dir() and list(path.glob("*_translated_*.json")):
            try: WORKSPACE.import_phase2(path)
            except (OSError, ValueError, json.JSONDecodeError) as error: QMessageBox.warning(self, "Cannot import results", str(error))
        else: self._open_inputs([path])

    def close_project(self) -> None:
        WORKSPACE.close(); self.landing.refresh_recent(); self.stack.setCurrentWidget(self.landing)

    def closeEvent(self, event) -> None:
        if self._import_thread is not None and self._import_thread.isRunning():
            self._import_thread.requestInterruption(); self._import_thread.quit(); self._import_thread.wait(3000)
        self.workspace.stop_thumbnail_loading()
        if WORKSPACE.pipeline.running:
            self._close_after_pipeline = True; WORKSPACE.cancel_pipeline(); event.ignore(); self.statusBar().showMessage("Cancelling pipeline before closing...")
            return
        WORKSPACE.save(); super().closeEvent(event)

    def _finish_pending_close(self, cancelled: bool) -> None:
        if self._close_after_pipeline:
            self._close_after_pipeline = False; QTimer.singleShot(0, self.close)
