"""Hydra Manga TL Phase 4 desktop translation editor."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFontDatabase, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QListWidget, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QSplitter, QTableWidget, QTableWidgetItem, QTextEdit, QToolBar, QVBoxLayout, QWidget,
)

from hydra_manga_tl.project.editor import EditorProject, RegionEdit
from hydra_manga_tl.phase.phase3 import run as render_phase3


class ImagePane(QScrollArea):
    def __init__(self, title: str):
        super().__init__()
        self.label = QLabel(f"{title}\nNo image loaded")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setMinimumSize(320, 400)
        self.setWidget(self.label)
        self.setWidgetResizable(True)
        self._path: Path | None = None

    def show_image(self, path: Path | None) -> None:
        self._path = path
        if path is None or not path.is_file():
            self.label.setText("No rendered preview yet")
            self.label.setPixmap(QPixmap())
            return
        pixmap = QPixmap(str(path))
        self.label.setPixmap(pixmap)
        self.label.resize(pixmap.size())
        self.setWidgetResizable(False)


class MainWindow(QMainWindow):
    project_changed = Signal()

    def __init__(self, initial_folder: Path | None = None):
        super().__init__()
        QFontDatabase.addApplicationFont(r"C:\Windows\Fonts\segoeui.ttf")
        self.setWindowTitle("Hydra Manga TL — Translation Editor")
        self.setStyleSheet("QWidget { font-family: 'Segoe UI'; font-size: 10pt; }")
        self.resize(1400, 850)
        self.setAcceptDrops(True)
        self.project: EditorProject | None = None
        self.project_path = Path("outputs/phase4/project.json").resolve()
        self.render_dir = Path("outputs/phase4/rendered").resolve()
        self.current_document = 0
        self.current_group = -1
        self._loading = False
        self._build_ui()
        if initial_folder and initial_folder.exists():
            self.open_phase2_folder(initial_folder)

    def _build_ui(self) -> None:
        toolbar = QToolBar("Project")
        self.addToolBar(toolbar)
        open_button = QPushButton("Open Phase 2 Folder")
        open_button.clicked.connect(self.choose_folder)
        project_button = QPushButton("Open Project")
        project_button.clicked.connect(self.choose_project)
        save_button = QPushButton("Save Project")
        save_button.clicked.connect(self.save_project)
        render_button = QPushButton("Render Selected")
        render_button.clicked.connect(self.render_selected)
        render_all_button = QPushButton("Render All")
        render_all_button.clicked.connect(self.render_all)
        for button in (open_button, project_button, save_button, render_button, render_all_button):
            toolbar.addWidget(button)

        root = QSplitter(Qt.Orientation.Horizontal)
        self.documents = QListWidget()
        self.documents.setMinimumWidth(230)
        self.documents.currentRowChanged.connect(self.select_document)
        root.addWidget(self.documents)

        center = QSplitter(Qt.Orientation.Vertical)
        previews = QSplitter(Qt.Orientation.Horizontal)
        self.original_pane = ImagePane("Original")
        self.translated_pane = ImagePane("Translated")
        previews.addWidget(self.original_pane)
        previews.addWidget(self.translated_pane)
        center.addWidget(previews)

        editor = QWidget()
        editor_layout = QHBoxLayout(editor)
        self.groups = QTableWidget(0, 4)
        self.groups.setHorizontalHeaderLabels(["#", "Status", "Original", "Translation"])
        self.groups.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.groups.itemSelectionChanged.connect(self.select_group)
        editor_layout.addWidget(self.groups, 3)

        form_host = QWidget()
        form = QFormLayout(form_host)
        self.translation = QTextEdit()
        self.translation.setMinimumHeight(110)
        self.replace = QCheckBox("Replace this text")
        self.font_size = QSpinBox(); self.font_size.setRange(0, 120); self.font_size.setSpecialValueText("Auto")
        self.offset_x = QSpinBox(); self.offset_x.setRange(-500, 500); self.offset_x.setSuffix(" px")
        self.offset_y = QSpinBox(); self.offset_y.setRange(-500, 500); self.offset_y.setSuffix(" px")
        apply_button = QPushButton("Apply Region Edit")
        apply_button.clicked.connect(self.apply_edit)
        form.addRow("Translated text", self.translation)
        form.addRow("Replacement", self.replace)
        form.addRow("Font size", self.font_size)
        form.addRow("Horizontal offset", self.offset_x)
        form.addRow("Vertical offset", self.offset_y)
        form.addRow(apply_button)
        editor_layout.addWidget(form_host, 1)
        center.addWidget(editor)
        root.addWidget(center)
        root.setStretchFactor(1, 1)
        self.setCentralWidget(root)
        self.statusBar().showMessage("Open or drop a Phase 2 output folder")

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Phase 2 output folder")
        if folder:
            self.open_phase2_folder(Path(folder))

    def choose_project(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Open editor project", str(self.project_path.parent), "Hydra Project (*.json)")
        if filename:
            self.open_project(Path(filename))

    def open_project(self, path: Path) -> None:
        try:
            project = EditorProject.load(path)
            for document in project.documents:
                if not Path(document.result_path).is_file():
                    raise FileNotFoundError(document.result_path)
        except (ValueError, FileNotFoundError, json.JSONDecodeError) as error:
            QMessageBox.warning(self, "Cannot open project", str(error))
            return
        self.project = project
        self.project_path = path.resolve()
        self.documents.clear()
        for document in project.documents:
            payload = json.loads(Path(document.result_path).read_text(encoding="utf-8"))
            self.documents.addItem(Path(payload["source"]).name)
        row = min(max(0, project.selected_document), len(project.documents) - 1)
        self.documents.setCurrentRow(row)
        self.statusBar().showMessage(f"Restored {len(project.documents)} images from {path.name}")

    def open_phase2_folder(self, folder: Path) -> None:
        try:
            self.project = EditorProject.from_phase2(folder)
        except ValueError as error:
            QMessageBox.warning(self, "Cannot open folder", str(error))
            return
        self.project_path = Path("outputs/phase4/project.json").resolve()
        self.documents.clear()
        for document in self.project.documents:
            payload = json.loads(Path(document.result_path).read_text(encoding="utf-8"))
            self.documents.addItem(Path(payload["source"]).name)
        self.documents.setCurrentRow(0)
        self.statusBar().showMessage(f"Loaded {len(self.project.documents)} images")

    def select_document(self, row: int) -> None:
        if self.project is None or row < 0:
            return
        self.current_document = row
        self.project.selected_document = row
        payload = self.project.effective_payload(row)
        self.original_pane.show_image(Path(payload["source"]))
        rendered = self.render_dir / f"{Path(payload['source']).stem}_translated_en.png"
        if not rendered.exists():
            fallback = Path("outputs/phase3") / rendered.name
            rendered = fallback if fallback.exists() else rendered
        self.translated_pane.show_image(rendered)
        self._populate_groups(payload)

    def _populate_groups(self, payload: dict) -> None:
        self._loading = True
        groups = payload["translation_groups"]
        self.groups.setRowCount(len(groups))
        for row, group in enumerate(groups):
            values = [str(group["index"]), group["status"], group["original_text"], group["translated_text"]]
            for column, value in enumerate(values):
                self.groups.setItem(row, column, QTableWidgetItem(value))
        self.groups.resizeColumnsToContents()
        self._loading = False
        if groups:
            self.groups.selectRow(0)

    def select_group(self) -> None:
        if self._loading or self.project is None:
            return
        row = self.groups.currentRow()
        if row < 0:
            return
        payload = self.project.effective_payload(self.current_document)
        group = payload["translation_groups"][row]
        self.current_group = group["index"]
        edit = self.project.documents[self.current_document].edits.get(str(self.current_group), RegionEdit())
        self.translation.setPlainText(group["translated_text"])
        self.replace.setChecked(edit.replace)
        self.font_size.setValue(edit.font_size)
        self.offset_x.setValue(edit.offset_x)
        self.offset_y.setValue(edit.offset_y)

    def apply_edit(self) -> None:
        if self.project is None or self.current_group < 0:
            return
        edit = RegionEdit(self.translation.toPlainText(), self.replace.isChecked(), self.font_size.value(), self.offset_x.value(), self.offset_y.value())
        self.project.update_edit(self.current_document, self.current_group, edit)
        self.groups.item(self.groups.currentRow(), 3).setText(edit.translated_text or "")
        self.project_changed.emit()
        self.statusBar().showMessage(f"Applied edit to group {self.current_group}")

    def save_project(self) -> None:
        if self.project is None:
            return
        self.project.save(self.project_path)
        self.statusBar().showMessage(f"Saved {self.project_path}")

    def _working_result(self, index: int) -> Path:
        assert self.project is not None
        working = Path("outputs/phase4/working").resolve()
        working.mkdir(parents=True, exist_ok=True)
        payload = self.project.effective_payload(index)
        source = Path(payload["source"])
        path = working / f"{source.stem}_translated_en.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def render_document(self, index: int) -> None:
        working = self._working_result(index)
        render_phase3(working, self.render_dir, policy="complete")

    def render_selected(self) -> None:
        if self.project is None:
            return
        self.apply_edit()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.render_document(self.current_document)
            self.save_project()
            self.select_document(self.current_document)
        finally:
            QApplication.restoreOverrideCursor()

    def render_all(self) -> None:
        if self.project is None:
            return
        self.apply_edit()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for index in range(len(self.project.documents)):
                self._working_result(index)
            render_phase3(Path("outputs/phase4/working").resolve(), self.render_dir, policy="complete")
            self.save_project()
            self.select_document(self.current_document)
        finally:
            QApplication.restoreOverrideCursor()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls()]
        folder = paths[0] if paths and paths[0].is_dir() else (paths[0].parent if paths else None)
        if folder:
            self.open_phase2_folder(folder)


def main() -> int:
    from hydra_manga_tl.core.application import MangaApplication
    initial = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    return MangaApplication(startup_path=initial).run()


if __name__ == "__main__":
    raise SystemExit(main())
