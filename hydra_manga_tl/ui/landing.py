"""Landing and import-progress screens."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QPixmap, QWheelEvent
from PySide6.QtWidgets import QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from hydra_manga_tl.core.assets import find_asset
from hydra_manga_tl.project.import_scan import ImportScanResult
from hydra_manga_tl.ui.shared import _landing_icon, _relative_opened_label
from hydra_manga_tl.project.workspace import WORKSPACE, RecentProjectSummary


class DropZone(QFrame):
    paths_dropped = Signal(list)
    import_folder_requested = Signal()
    images_requested = Signal()
    project_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(7)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = QLabel()
        icon.setObjectName("DropIcon")
        icon.setPixmap(_landing_icon("folder", 48))
        icon.setFixedSize(52, 52)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("Drop manga images or a folder here")
        title.setObjectName("DropTitle")
        self.import_button = QPushButton("+  Import Manga")
        self.import_button.setObjectName("LandingPrimary")
        self.import_button.setMinimumWidth(190)
        self.import_button.clicked.connect(self.import_folder_requested)
        secondary = QHBoxLayout(); secondary.setSpacing(5)
        self.images_button = QPushButton("Add Images")
        self.images_button.setObjectName("SecondaryLink")
        self.images_button.clicked.connect(self.images_requested)
        divider = QLabel("|"); divider.setObjectName("ActionDivider")
        self.project_button = QPushButton("Open Project")
        self.project_button.setObjectName("SecondaryLink")
        self.project_button.clicked.connect(self.project_requested)
        secondary.addStretch(); secondary.addWidget(self.images_button); secondary.addWidget(divider); secondary.addWidget(self.project_button); secondary.addStretch()
        subtitle = QLabel("JPG, PNG, WEBP, TIFF, BMP  •  Original images are never modified")
        subtitle.setObjectName("DropMeta")
        layout.addWidget(icon, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.import_button, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(secondary)
        layout.addWidget(subtitle, alignment=Qt.AlignmentFlag.AlignCenter)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            self.setProperty("dragActive", True); self.style().polish(self)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self.setProperty("dragActive", False); self.style().polish(self)

    def dropEvent(self, event: QDropEvent) -> None:
        self.setProperty("dragActive", False); self.style().polish(self)
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls()]
        if paths:
            self.paths_dropped.emit(paths)


class RecentProjectCard(QFrame):
    activated = Signal(Path)
    remove_requested = Signal(Path)
    scroll_requested = Signal(int)

    def __init__(self, summary: RecentProjectSummary) -> None:
        super().__init__()
        self.summary = summary
        self.setObjectName("RecentProjectCard")
        self.setProperty("focused", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(str(summary.path))
        self.setAccessibleName(f"Open {summary.name}")
        self.setFixedSize(300, 112)
        row = QHBoxLayout(self); row.setContentsMargins(15, 13, 15, 13); row.setSpacing(13)
        icon_tile = QFrame(); icon_tile.setObjectName("RecentIconTile"); icon_tile.setFixedSize(54, 54)
        icon_tile.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        icon_layout = QVBoxLayout(icon_tile); icon_layout.setContentsMargins(8, 8, 8, 8)
        icon = QLabel(); icon.setPixmap(_landing_icon("book", 36)); icon.setAlignment(Qt.AlignmentFlag.AlignCenter); icon_layout.addWidget(icon)
        details = QVBoxLayout(); details.setSpacing(2)
        self.title_label = QLabel(summary.name); self.title_label.setObjectName("RecentProjectTitle")
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        title_row = QHBoxLayout(); title_row.setSpacing(5)
        title_row.addWidget(self.title_label, 1)
        self.remove_button = QPushButton("×")
        self.remove_button.setObjectName("RecentRemove")
        self.remove_button.setFixedSize(24, 24)
        self.remove_button.setToolTip("Remove from recent projects")
        self.remove_button.setAccessibleName(f"Remove {summary.name} from recent projects")
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self.summary.path))
        title_row.addWidget(self.remove_button, alignment=Qt.AlignmentFlag.AlignTop)
        self.language_label = QLabel(f"{summary.source_language} → {summary.target_language}"); self.language_label.setObjectName("RecentProjectMeta")
        page_word = "page" if summary.page_count == 1 else "pages"
        self.pages_label = QLabel(f"{summary.page_count} {page_word}"); self.pages_label.setObjectName("RecentProjectMeta")
        self.opened_label = QLabel(_relative_opened_label(summary.last_opened)); self.opened_label.setObjectName("RecentOpened")
        details.addLayout(title_row)
        for label in (self.language_label, self.pages_label, self.opened_label):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            details.addWidget(label)
        row.addWidget(icon_tile); row.addLayout(details, 1)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.activated.emit(self.summary.path)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self.summary.path)
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y() or event.angleDelta().x() or event.pixelDelta().y() or event.pixelDelta().x()
        if delta:
            self.scroll_requested.emit(delta)
            event.accept()
            return
        super().wheelEvent(event)

    def focusInEvent(self, event) -> None:
        self.setProperty("focused", True); self.style().unpolish(self); self.style().polish(self)
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self.setProperty("focused", False); self.style().unpolish(self); self.style().polish(self)
        super().focusOutEvent(event)


class RecentProjectsScrollArea(QScrollArea):
    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y() or event.angleDelta().x() or event.pixelDelta().y() or event.pixelDelta().x()
        bar = self.horizontalScrollBar()
        if delta and bar.maximum() > 0:
            bar.setValue(bar.value() - delta)
            event.accept()
            return
        super().wheelEvent(event)


class LandingScreen(QWidget):
    inputs_selected = Signal(list)
    project_selected = Signal(Path)

    def __init__(self) -> None:
        super().__init__()
        banner_path = find_asset("logos", "mainlogo.png")
        self._banner_source = QPixmap(str(banner_path)) if banner_path else QPixmap()
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 18, 32, 18)
        self.content = QWidget(); self.content.setObjectName("LandingContent"); self.content.setMaximumWidth(1320)
        self.content.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        column = QVBoxLayout(self.content); column.setContentsMargins(0, 0, 0, 0); column.setSpacing(7)
        self.banner = QLabel()
        self.banner.setObjectName("Banner")
        self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.banner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        column.addWidget(self.banner, alignment=Qt.AlignmentFlag.AlignCenter)
        product_title = QLabel("AI Manga Translation Studio"); product_title.setObjectName("LandingHeroTitle"); product_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        description = QLabel("Translate manga pages while preserving artwork, speech bubbles and layout."); description.setObjectName("LandingDescription"); description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(product_title)
        column.addWidget(description)
        column.addSpacing(10)
        self.drop = DropZone()
        self.drop.paths_dropped.connect(self.inputs_selected)
        self.drop.import_folder_requested.connect(self._choose_folder)
        self.drop.images_requested.connect(self._choose_images)
        self.drop.project_requested.connect(self._choose_project)
        column.addWidget(self.drop)
        column.addSpacing(9)
        recent_header = QHBoxLayout(); recent_header.setSpacing(8)
        recent_label = QLabel("Recent Projects"); recent_label.setObjectName("RecentHeading")
        self.clear_history_button = QPushButton("Clear History")
        self.clear_history_button.setObjectName("ClearHistory")
        self.clear_history_button.clicked.connect(self._confirm_clear_history)
        recent_header.addWidget(recent_label); recent_header.addStretch(); recent_header.addWidget(self.clear_history_button)
        column.addLayout(recent_header)
        self.recent_scroll = RecentProjectsScrollArea(); self.recent_scroll.setObjectName("RecentProjectsScroll")
        self.recent_scroll.setWidgetResizable(True)
        self.recent_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.recent_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.recent_host = QWidget(); self.recent_host.setObjectName("RecentProjectsHost")
        self.recent_layout = QHBoxLayout(self.recent_host); self.recent_layout.setContentsMargins(0, 0, 0, 0); self.recent_layout.setSpacing(12)
        self.recent_scroll.setWidget(self.recent_host)
        column.addWidget(self.recent_scroll)
        root.addWidget(self.content, alignment=Qt.AlignmentFlag.AlignHCenter)
        root.addStretch(1)
        self.recent_cards: list[RecentProjectCard] = []
        self.refresh_recent()
        self._update_banner()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.content.setFixedWidth(max(640, min(1320, self.width() - 64)))
        self._update_banner()

    def _update_banner(self) -> None:
        if self._banner_source.isNull():
            self.banner.clear()
            return
        compact = self.height() < 800
        target_height = 118 if compact else 180
        available_width = max(360, min(720, self.width() - 120))
        scaled = self._banner_source.scaled(
            available_width, target_height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.banner.setPixmap(scaled)
        self.banner.setFixedSize(scaled.size())
        self.drop.setFixedHeight(185 if compact else 220)
        self.recent_scroll.setFixedHeight(116 if compact else 126)

    def refresh_recent(self) -> None:
        while self.recent_layout.count():
            item = self.recent_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.recent_cards = []
        summaries = WORKSPACE.recent_project_summaries()
        for summary in summaries:
            card = RecentProjectCard(summary)
            card.activated.connect(self.project_selected)
            card.remove_requested.connect(self._remove_recent_project)
            card.scroll_requested.connect(self._scroll_recent)
            self.recent_cards.append(card)
            self.recent_layout.addWidget(card)
        if not summaries:
            empty = QLabel("No recent projects yet  •  Imported projects will appear here")
            empty.setObjectName("EmptyRecent"); empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.recent_layout.addWidget(empty, 1)
        self.recent_layout.addStretch(1)
        minimum_width = len(summaries) * 300 + max(0, len(summaries) - 1) * 12
        self.recent_host.setMinimumWidth(minimum_width)
        self.clear_history_button.setEnabled(bool(summaries))

    def _scroll_recent(self, delta: int) -> None:
        bar = self.recent_scroll.horizontalScrollBar()
        bar.setValue(bar.value() - delta)

    def _remove_recent_project(self, path: Path) -> None:
        data_root = WORKSPACE.recent_project_data_root(path)
        if data_root is None:
            answer = QMessageBox.question(
                self,
                "Remove Recent Project?",
                (
                    "Remove this project from recent history?\n\n"
                    "No project files or exported files will be deleted."
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            WORKSPACE.forget_recent_project(path)
            self.refresh_recent()
            return
        answer = QMessageBox.question(
            self,
            "Delete Recent Project Data?",
            (
                "Remove this project from recent history and delete its "
                "Hydra project data folder?\n\n"
                f"{data_root}\n\n"
                "Exported files outside Hydra project data will not be deleted."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            WORKSPACE.delete_recent_project_data(path)
            WORKSPACE.forget_recent_project(path)
        except OSError as error:
            QMessageBox.warning(self, "Project data delete failed", str(error))
            return
        self.refresh_recent()

    def _confirm_clear_history(self) -> None:
        recent = WORKSPACE.recent_projects()
        deletable = [
            root
            for root in (WORKSPACE.recent_project_data_root(path) for path in recent)
            if root is not None
        ]
        folder_list = "\n".join(str(root) for root in deletable) or "No Hydra project data folders found."
        answer = QMessageBox.question(
            self,
            "Clear Recent Projects?",
            (
                "Remove all recent-project shortcuts and delete Hydra project "
                f"data for {len(deletable)} project(s)?\n\n"
                f"{folder_list}\n\n"
                "Exported files outside Hydra project data will not be deleted."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            for path in recent:
                WORKSPACE.delete_recent_project_data(path)
            WORKSPACE.clear_recent_projects()
        except OSError as error:
            QMessageBox.warning(self, "Project data delete failed", str(error))
            return
        self.refresh_recent()

    def _choose_images(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Add manga images", "", "Images (*.jpg *.jpeg *.png *.webp *.tif *.tiff *.bmp)")
        if files: self.inputs_selected.emit([Path(value) for value in files])

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add manga folder")
        if folder: self.inputs_selected.emit([Path(folder)])

    def _choose_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open Hydra Manga project", "", "Hydra Manga Project (project.json)")
        if path: self.project_selected.emit(Path(path))


class ImportProgressScreen(QWidget):
    """Responsive, truthful project-preparation view shown during folder import."""

    _STAGES = ("detecting", "metadata", "preparing", "previews")
    _LABELS = {
        "detecting": "Detecting supported images",
        "metadata": "Reading file metadata",
        "preparing": "Preparing the project",
        "previews": "Loading previews",
    }

    def __init__(self) -> None:
        super().__init__()
        root = QVBoxLayout(self); root.setContentsMargins(80, 60, 80, 60)
        root.addStretch()
        card = QFrame(); card.setObjectName("ImportCard"); card.setMaximumWidth(720)
        layout = QVBoxLayout(card); layout.setContentsMargins(30, 26, 30, 26); layout.setSpacing(12)
        title = QLabel("Preparing Translation Project"); title.setObjectName("Heading"); layout.addWidget(title)
        self.project_name = QLabel(); self.project_name.setObjectName("Muted"); layout.addWidget(self.project_name)
        layout.addSpacing(5)
        self.stage_labels: dict[str, QLabel] = {}
        for stage in self._STAGES:
            label = QLabel(f"○  {self._LABELS[stage]}")
            self.stage_labels[stage] = label; layout.addWidget(label)
        self.progress = QProgressBar(); self.progress.setTextVisible(True); layout.addWidget(self.progress)
        self.detail = QLabel(); self.detail.setObjectName("Muted"); self.detail.setWordWrap(True); layout.addWidget(self.detail)
        self.summary = QLabel(); self.summary.setObjectName("ImportSummary"); self.summary.setWordWrap(True); layout.addWidget(self.summary)
        host = QHBoxLayout(); host.addStretch(); host.addWidget(card); host.addStretch(); root.addLayout(host)
        root.addStretch()

    def begin(self, paths: list[Path]) -> None:
        name = WORKSPACE._default_project_name(paths)
        self.project_name.setText(name)
        self.summary.clear()
        self.update_progress("detecting", 0, 0, "Scanning folders…")

    def update_progress(self, stage: str, current: int, total: int, detail: str) -> None:
        active_index = self._STAGES.index(stage)
        for index, value in enumerate(self._STAGES):
            marker = "DONE" if index < active_index else ("NOW " if index == active_index else "    ")
            self.stage_labels[value].setText(f"{marker}  {self._LABELS[value]}")
            self.stage_labels[value].setProperty("active", index == active_index)
            self.stage_labels[value].style().polish(self.stage_labels[value])
        if total > 0:
            self.progress.setRange(0, total); self.progress.setValue(current)
            self.progress.setFormat(f"{current} / {total}")
        else:
            self.progress.setRange(0, 0); self.progress.setFormat("")
        self.detail.setText(detail)

    def show_result(self, result: ImportScanResult) -> None:
        format_text = "  •  ".join(f"{name} {count}" for name, count in sorted(result.formats.items()))
        size = self._format_bytes(result.total_bytes)
        skipped = f"  •  {len(result.unreadable)} unreadable skipped" if result.unreadable else ""
        self.summary.setText(
            f"{result.image_count} images  •  {size}  •  Average {result.average_width} × {result.average_height} px\n"
            f"{format_text}{skipped}"
        )
        self.update_progress("preparing", 0, 0, "Creating the project…")

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(value)
        for unit in ("B", "KB", "MB", "GB"):
            if amount < 1024 or unit == "GB":
                return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
            amount /= 1024
        return f"{amount:.1f} GB"
