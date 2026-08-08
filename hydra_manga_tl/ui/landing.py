"""Landing and import-progress screens."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QSpacerItem, QVBoxLayout, QWidget
)

from hydra_manga_tl.core.assets import find_asset
from hydra_manga_tl.core.paths import PATHS
from hydra_manga_tl.core.settings import SETTINGS
from hydra_manga_tl.core.updater import STATUS_AVAILABLE, STATUS_CHECKING, STATUS_FAILED, UPDATER, UpdateState
from hydra_manga_tl.core.user_errors import workspace_action_error
from hydra_manga_tl.project.import_scan import ImportScanResult
from hydra_manga_tl.ui.shared import _landing_icon, _relative_opened_label, lucide_icon
from hydra_manga_tl.project.workspace import WORKSPACE, RecentProjectSummary


LANDING_RECENT_VISIBLE_LIMIT = 5
RECENT_DIALOG_CARD_WIDTH = 342
RECENT_DIALOG_GRID_SPACING = 8


def configured_project_import_root() -> Path:
    configured = str(getattr(SETTINGS, "project_import_root", "") or "").strip()
    if configured:
        try:
            path = Path(configured).expanduser()
            if path.exists() and path.is_dir():
                return path
        except (OSError, RuntimeError, ValueError):
            pass
    return PATHS.projects


def configured_manga_import_root() -> Path:
    configured = str(getattr(SETTINGS, "manga_import_root", "") or "").strip()
    if configured:
        try:
            path = Path(configured).expanduser()
            if path.exists() and path.is_dir():
                return path
        except (OSError, RuntimeError, ValueError):
            pass
    return Path.home()


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
        self.import_button.setIcon(lucide_icon("image-plus"))
        self.import_button.clicked.connect(self.import_folder_requested)
        
        secondary = QHBoxLayout()
        secondary.setSpacing(5)
        self.images_button = QPushButton("Add Images")
        self.images_button.setObjectName("SecondaryLink")
        self.images_button.setIcon(lucide_icon("image-plus"))
        self.images_button.clicked.connect(self.images_requested)
        
        divider = QLabel("|")
        divider.setObjectName("ActionDivider")
        
        self.project_button = QPushButton("Open Project")
        self.project_button.setObjectName("SecondaryLink")
        self.project_button.setIcon(lucide_icon("folder-open"))
        self.project_button.clicked.connect(self.project_requested)
        
        secondary.addStretch()
        secondary.addWidget(self.images_button)
        secondary.addWidget(divider)
        secondary.addWidget(self.project_button)
        secondary.addStretch()
        
        subtitle = QLabel("JPG, PNG, WEBP, TIFF, BMP  •  Original images are never modified")
        subtitle.setObjectName("DropMeta")
        
        layout.addWidget(icon, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.import_button, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(secondary)
        layout.addWidget(subtitle, alignment=Qt.AlignmentFlag.AlignCenter)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            self.setProperty("dragActive", True)
            self.style().polish(self)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self.setProperty("dragActive", False)
        self.style().polish(self)

    def dropEvent(self, event: QDropEvent) -> None:
        self.setProperty("dragActive", False)
        self.style().polish(self)
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
        
        tooltip = str(summary.path)
        if summary.compatibility_message:
            tooltip += f"\n\n{summary.compatibility_message}"
        self.setToolTip(tooltip)
        self.setAccessibleName(f"Open {summary.name}")
        self.setFixedSize(342, 162)
        
        row = QHBoxLayout(self)
        row.setContentsMargins(15, 13, 15, 13)
        row.setSpacing(13)
        
        icon_tile = QFrame()
        icon_tile.setObjectName("RecentIconTile")
        icon_tile.setFixedSize(76, 116)
        icon_tile.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        
        icon_layout = QVBoxLayout(icon_tile)
        icon_layout.setContentsMargins(7, 7, 7, 7)
        icon = QLabel()
        thumbnail_path = summary.thumbnail_path or find_asset("thumbnail", "hydra.png")
        thumbnail = QPixmap(str(thumbnail_path)) if thumbnail_path else QPixmap()
        icon.setPixmap(
            thumbnail.scaled(
                62,
                84,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            if not thumbnail.isNull()
            else _landing_icon("book", 42)
        )
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_layout.addWidget(icon)
        
        details = QVBoxLayout()
        details.setSpacing(2)
        
        self.title_label = QLabel(summary.name)
        self.title_label.setObjectName("RecentProjectTitle")
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        
        title_row = QHBoxLayout()
        title_row.setSpacing(5)
        title_row.addWidget(self.title_label, 1)
        
        self.remove_button = QPushButton()
        self.remove_button.setObjectName("RecentRemove")
        self.remove_button.setFixedSize(24, 24)
        self.remove_button.setIcon(lucide_icon("x"))
        self.remove_button.setToolTip("Remove from recent projects")
        self.remove_button.setAccessibleName(f"Remove {summary.name} from recent projects")
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self.summary.path))
        title_row.addWidget(self.remove_button, alignment=Qt.AlignmentFlag.AlignTop)
        
        self.language_label = QLabel(f"{summary.source_language} → {summary.target_language}")
        self.language_label.setObjectName("RecentMetaChip")
        
        page_word = "page" if summary.page_count == 1 else "pages"
        self.pages_label = QLabel(f"{summary.page_count} {page_word}")
        self.pages_label.setObjectName("RecentMetaChip")
              
        state_text = summary.state_display or summary.state_label or "Not Started"
        self.state_label = QLabel(state_text)
        self.state_label.setObjectName("RecentStateLine")

        if summary.exported:
            _export_type_display = {
                "folder": "Exported Folder",
                "archive": "Exported Archive",
                "pdf": "Exported PDF",
            }.get(summary.export_type, "Exported")
            _parts = [_export_type_display]
            if summary.export_count:
                _pg = "page" if summary.export_count == 1 else "pages"
                _parts.append(f"{summary.export_count} {_pg}")
            if summary.export_relative_time:
                _parts.append(summary.export_relative_time)
            export_text = " • ".join(_parts)
        else:
            export_text = "Not exported yet"
        self.export_label = QLabel(export_text)
        self.export_label.setObjectName("RecentExportLine")

        status_text = {
            "compatible": "Compatible",
            "migration_required": "Upgrade required • backup will be created",
            "incompatible": f"⚠ Requires Hydra {summary.minimum_app_version}",
            "unsupported": "⚠ Unsupported project schema",
            "invalid": "⚠ Project metadata is invalid",
        }.get(summary.compatibility_status, summary.compatibility_status.title())
        
        self.compatibility_label = QLabel(status_text)
        self.compatibility_label.setObjectName(
            "RecentOpened"
            if summary.compatibility_status == "compatible"
            else "RecentCompatibilityWarning"
        )
        
        self.opened_label = QLabel(_relative_opened_label(summary.last_opened))
        self.opened_label.setObjectName("RecentOpened")
        
        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(0, 2, 0, 1)
        meta_row.setSpacing(6)
        meta_row.addWidget(self.pages_label)
        meta_row.addWidget(self.language_label)
        meta_row.addStretch(1)

        details.addLayout(title_row)
        details.addLayout(meta_row)
        for label in (
            self.state_label,
            self.export_label,
            self.compatibility_label,
            self.opened_label,
        ):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            details.addWidget(label)

        row.addWidget(icon_tile)
        row.addLayout(details, 1)

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
        self.setProperty("focused", True)
        self.style().unpolish(self)
        self.style().polish(self)
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self.setProperty("focused", False)
        self.style().unpolish(self)
        self.style().polish(self)
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


def confirm_remove_recent_project(parent: QWidget, path: Path) -> bool:
    data_root = WORKSPACE.recent_project_data_root(path)
    if data_root is None:
        answer = QMessageBox.question(
            parent,
            "Remove Recent Project?",
            (
                "Remove this project from recent history?\n\n"
                "No project files or exported files will be deleted."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        WORKSPACE.forget_recent_project(path)
        return True

    answer = QMessageBox.question(
        parent,
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
        return False
    WORKSPACE.delete_recent_project_data(path)
    WORKSPACE.forget_recent_project(path)
    return True


class RecentProjectsDialog(QDialog):
    project_selected = Signal(Path)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Recent Projects")
        self.resize(1080, 800)
        self.setMinimumSize(720, 520)
        self._summaries: list[RecentProjectSummary] = []
        self._cards: list[RecentProjectCard] = []
        self._last_columns = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Recent Projects")
        title.setObjectName("RecentHeading")
        header.addWidget(title)
        header.addStretch(1)
        close = QPushButton("Close")
        close.setIcon(lucide_icon("x"))
        close.clicked.connect(self.reject)
        header.addWidget(close)
        root.addLayout(header)

        self.search = QLineEdit()
        self.search.setObjectName("RecentSearch")
        self.search.setPlaceholderText("Search recent projects")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(lucide_icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._refresh_grid)
        root.addWidget(self.search)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("RecentProjectsDialogScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.host = QWidget()
        self.host.setObjectName("RecentProjectsDialogHost")
        self.grid = QGridLayout(self.host)
        self.grid.setContentsMargins(0, 4, 0, 4)
        self.grid.setHorizontalSpacing(RECENT_DIALOG_GRID_SPACING)
        self.grid.setVerticalSpacing(RECENT_DIALOG_GRID_SPACING)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll.setWidget(self.host)
        root.addWidget(self.scroll, 1)

        self.empty = QLabel("No matching recent projects")
        self.empty.setObjectName("EmptyRecent")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setMinimumHeight(120)

        self._load_summaries()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        columns = self._grid_columns()
        if columns != self._last_columns:
            self._refresh_grid()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        columns = self._grid_columns()
        if columns != self._last_columns:
            self._refresh_grid()

    @staticmethod
    def _matches_summary(summary: RecentProjectSummary, query: str) -> bool:
        if not query:
            return True
        haystack = " ".join(
            (
                summary.name,
                str(summary.path),
                summary.source_language,
                summary.target_language,
            )
        ).casefold()
        return query.casefold() in haystack

    def _filtered_summaries(self) -> list[RecentProjectSummary]:
        query = self.search.text().strip()
        return [summary for summary in self._summaries if self._matches_summary(summary, query)]

    def _grid_columns(self) -> int:
        viewport_width = self.scroll.viewport().width() if hasattr(self, "scroll") else 0
        width = max(viewport_width, self.width() - 48)
        three_columns = RECENT_DIALOG_CARD_WIDTH * 3 + RECENT_DIALOG_GRID_SPACING * 2
        two_columns = RECENT_DIALOG_CARD_WIDTH * 2 + RECENT_DIALOG_GRID_SPACING
        if width >= three_columns:
            return 3
        if width >= two_columns:
            return 2
        return 1

    def _load_summaries(self) -> None:
        self._summaries = WORKSPACE.recent_project_summaries()
        self._refresh_grid()

    def _clear_grid(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget() is self.empty:
                self.empty.setParent(None)
            elif item.widget() is not None:
                widget = item.widget()
                widget.setParent(None)
                widget.deleteLater()
            elif isinstance(item, QSpacerItem):
                del item
        self._cards.clear()

    def _refresh_grid(self) -> None:
        self._clear_grid()
        summaries = self._filtered_summaries()
        if not summaries:
            self.grid.addWidget(self.empty, 0, 0)
            return
        columns = self._grid_columns()
        self._last_columns = columns
        for index, summary in enumerate(summaries):
            card = RecentProjectCard(summary)
            card.activated.connect(self._activate_project)
            card.remove_requested.connect(self._remove_recent_project)
            self._cards.append(card)
            self.grid.addWidget(card, index // columns, index % columns)

    def _activate_project(self, path: Path) -> None:
        self.project_selected.emit(path)
        self.accept()

    def _remove_recent_project(self, path: Path) -> None:
        try:
            if confirm_remove_recent_project(self, path):
                self._load_summaries()
        except OSError as error:
            QMessageBox.warning(
                self,
                "Project data delete failed",
                workspace_action_error(error, action="delete project data"),
            )


class UpdateCard(QFrame):
    """Compact landing-page update affordance."""

    def __init__(self) -> None:
        super().__init__()
        self._state = UpdateState()
        self.setObjectName("UpdateCard")
        self.setMaximumWidth(360)
        self.setMinimumWidth(320)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.icon = QLabel()
        self.icon.setObjectName("UpdateIcon")
        self.icon.setFixedSize(34, 34)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon.setPixmap(lucide_icon("download").pixmap(20, 20))

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        self.title = QLabel("Update available")
        self.title.setObjectName("UpdateTitle")
        self.detail = QLabel("")
        self.detail.setObjectName("Muted")
        self.detail.setWordWrap(True)
        title_col.addWidget(self.title)
        title_col.addWidget(self.detail)

        self.later_button = QPushButton()
        self.later_button.setObjectName("RecentRemove")
        self.later_button.setFixedSize(24, 24)
        self.later_button.setIcon(lucide_icon("x"))
        self.later_button.setToolTip("Remind me later")
        self.later_button.clicked.connect(self._dismiss)

        header.addWidget(self.icon)
        header.addLayout(title_col, 1)
        header.addWidget(self.later_button, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.download_button = QPushButton("Download Update")
        self.download_button.setObjectName("Primary")
        self.download_button.setIcon(lucide_icon("download"))
        self.download_button.clicked.connect(self._download)
        self.hide_button = QPushButton("Later")
        self.hide_button.clicked.connect(self._dismiss)
        actions.addWidget(self.download_button, 1)
        actions.addWidget(self.hide_button)
        layout.addLayout(actions)

        self.apply_state(self._state)

    def apply_state(self, state: UpdateState) -> None:
        self._state = state
        if state.status == STATUS_CHECKING:
            self.title.setText("Checking for updates")
            self.detail.setText("Looking for the latest Hydra Manga TL release.")
            self.download_button.setVisible(False)
            self.hide_button.setText("Hide")
            self.setVisible(True)
            return
        if state.status == STATUS_AVAILABLE and not state.dismissed:
            self.title.setText("Update available")
            self.detail.setText(f"Hydra Manga TL {state.latest_version} is ready.")
            self.download_button.setVisible(True)
            self.hide_button.setText("Later")
            self.setVisible(True)
            return
        if state.status == STATUS_FAILED and state.reason == "manual":
            self.title.setText("Update check failed")
            self.detail.setText("Could not check for updates. Please try again.")
            self.download_button.setVisible(False)
            self.hide_button.setText("Hide")
            self.setVisible(True)
            return
        self.setVisible(False)

    def _download(self) -> None:
        if not self._state.url:
            return
        if SETTINGS.updates_prompt_before_download:
            answer = QMessageBox.question(
                self,
                "Download Update?",
                (
                    f"Download Hydra Manga TL {self._state.latest_version}?\n\n"
                    f"File: {self._state.file_name}\n"
                    "The installer will open in your browser or download manager."
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        QDesktopServices.openUrl(QUrl(self._state.url))

    def _dismiss(self) -> None:
        if self._state.status == STATUS_AVAILABLE and self._state.latest_version:
            UPDATER.dismiss_available_update()
        else:
            self.setVisible(False)


class LandingScreen(QWidget):
    inputs_selected = Signal(list)
    project_selected = Signal(Path)

    def __init__(self) -> None:
        super().__init__()
        banner_path = find_asset("logos", "mainlogo.png")
        self._banner_source = QPixmap(str(banner_path)) if banner_path else QPixmap()
        
        root = QVBoxLayout(self)
        root.setContentsMargins(34, 20, 34, 20)
        
        self.content = QWidget()
        self.content.setObjectName("LandingContent")
        self.content.setMaximumWidth(1320)
        self.content.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        
        column = QVBoxLayout(self.content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        
        self.banner = QLabel()
        self.banner.setObjectName("Banner")
        self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.banner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        column.addWidget(self.banner, alignment=Qt.AlignmentFlag.AlignCenter)
        
        product_title = QLabel("AI Manga Translation Studio")
        product_title.setObjectName("LandingHeroTitle")
        product_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        description = QLabel("Translate manga pages while preserving artwork, speech bubbles and layout.")
        description.setObjectName("LandingDescription")
        description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        column.addWidget(product_title)
        column.addWidget(description)
        column.addSpacing(12)
        
        self.drop = DropZone()
        self.drop.paths_dropped.connect(self.inputs_selected)
        self.drop.import_folder_requested.connect(self._choose_folder)
        self.drop.images_requested.connect(self._choose_images)
        self.drop.project_requested.connect(self._choose_project)
        column.addWidget(self.drop)
        column.addSpacing(12)
        
        recent_header = QHBoxLayout()
        recent_header.setSpacing(8)
        recent_label = QLabel("Recent Projects")
        recent_label.setObjectName("RecentHeading")
        
        self.clear_history_button = QPushButton("Clear History")
        self.clear_history_button.setObjectName("ClearHistory")
        self.clear_history_button.setIcon(lucide_icon("trash-2"))
        self.clear_history_button.clicked.connect(self._confirm_clear_history)

        self.view_all_button = QPushButton("View All")
        self.view_all_button.setObjectName("ViewAllRecent")
        self.view_all_button.setIcon(lucide_icon("clock"))
        self.view_all_button.clicked.connect(self._view_all_recent)
        
        recent_header.addWidget(recent_label)
        recent_header.addStretch()
        recent_header.addWidget(self.view_all_button)
        recent_header.addWidget(self.clear_history_button)
        column.addLayout(recent_header)
        
        self.recent_scroll = RecentProjectsScrollArea()
        self.recent_scroll.setObjectName("RecentProjectsScroll")
        self.recent_scroll.setWidgetResizable(True)
        self.recent_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.recent_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        
        self.recent_host = QWidget()
        self.recent_host.setObjectName("RecentProjectsHost")
        self.recent_layout = QHBoxLayout(self.recent_host)
        
        # Increased margin slightly to prevent clipping on the scroll boundary
        self.recent_layout.setContentsMargins(4, 4, 4, 4)
        self.recent_layout.setSpacing(12)
        self.recent_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        
        self.recent_scroll.setWidget(self.recent_host)
        column.addWidget(self.recent_scroll)

        update_row = QHBoxLayout()
        update_row.setContentsMargins(0, 2, 0, 0)
        update_row.addStretch()
        self.update_card = UpdateCard()
        update_row.addWidget(self.update_card)
        column.addLayout(update_row)
        UPDATER.update_state_changed.connect(self.update_card.apply_state)
        self.update_card.apply_state(UPDATER.current_state())
        
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
        self.recent_scroll.setFixedHeight(150 if compact else 160)

    def refresh_recent(self) -> None:
        # Properly clean up previous items including stretch spacers
        while self.recent_layout.count():
            item = self.recent_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
            elif isinstance(item, QSpacerItem):
                del item

        self.recent_cards.clear()
        all_summaries = WORKSPACE.recent_project_summaries()
        summaries = all_summaries[:LANDING_RECENT_VISIBLE_LIMIT]
        
        for summary in summaries:
            card = RecentProjectCard(summary)
            card.activated.connect(self.project_selected)
            card.remove_requested.connect(self._remove_recent_project)
            card.scroll_requested.connect(self._scroll_recent)
            self.recent_cards.append(card)
            self.recent_layout.addWidget(card)
            
        if not summaries:
            empty = QLabel("No recent projects yet  •  Imported projects will appear here")
            empty.setObjectName("EmptyRecent")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.recent_layout.addWidget(empty, 1)
        else:
            self.recent_layout.addStretch(1)

        # FIXED width calculation: Use actual 320px width of the card instead of 300px
        # Added extra buffer for layout margins
        minimum_width = len(summaries) * 342 + max(0, len(summaries) - 1) * 12 + 16
        self.recent_host.setMinimumWidth(minimum_width)
        self.view_all_button.setEnabled(bool(all_summaries))
        self.clear_history_button.setEnabled(bool(all_summaries))

    def _scroll_recent(self, delta: int) -> None:
        bar = self.recent_scroll.horizontalScrollBar()
        bar.setValue(bar.value() - delta)

    def _remove_recent_project(self, path: Path) -> None:
        try:
            removed = confirm_remove_recent_project(self, path)
        except OSError as error:
            QMessageBox.warning(
                self,
                "Project data delete failed",
                workspace_action_error(error, action="delete project data"),
            )
            return
        if removed:
            self.refresh_recent()

    def _view_all_recent(self) -> None:
        dialog = RecentProjectsDialog(self)
        dialog.project_selected.connect(self.project_selected)
        dialog.exec()
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
            QMessageBox.warning(
                self,
                "Project data delete failed",
                workspace_action_error(error, action="delete project data"),
            )
            return
        self.refresh_recent()

    def _choose_images(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Add manga images", "", "Images (*.jpg *.jpeg *.png *.webp *.tif *.tiff *.bmp)")
        if files:
            self.inputs_selected.emit([Path(value) for value in files])

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Add manga folder",
            str(configured_manga_import_root()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if folder:
            self.inputs_selected.emit([Path(folder)])

    def _choose_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Hydra Manga project",
            str(configured_project_import_root()),
            "Hydra Manga Project (project.json)",
        )
        if path:
            self.project_selected.emit(Path(path))


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
        root = QVBoxLayout(self)
        root.setContentsMargins(80, 54, 80, 54)
        root.addStretch()
        
        card = QFrame()
        card.setObjectName("ImportCard")
        card.setMaximumWidth(780)
        
        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        logo = QLabel()
        logo.setObjectName("ImportLogo")
        logo.setFixedSize(58, 58)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_path = find_asset("thumbnail", "hydra.png")
        logo_pixmap = QPixmap(str(logo_path)) if logo_path else QPixmap()
        if not logo_pixmap.isNull():
            logo.setPixmap(
                logo_pixmap.scaled(
                    52,
                    52,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

        title_column = QVBoxLayout()
        title_column.setSpacing(3)
        title = QLabel("Preparing Translation Project")
        title.setObjectName("ImportTitle")
        subtitle = QLabel("Hydra is scanning sources and building a safe local workspace.")
        subtitle.setObjectName("Muted")
        title_column.addWidget(title)
        title_column.addWidget(subtitle)
        header.addWidget(logo)
        header.addLayout(title_column, 1)
        layout.addLayout(header)
        
        self.project_name = QLabel()
        self.project_name.setObjectName("ImportProjectName")
        layout.addWidget(self.project_name)
        
        self.stage_labels: dict[str, QLabel] = {}
        self.stage_marks: dict[str, QLabel] = {}
        self.stage_rows: dict[str, QFrame] = {}
        stages = QFrame()
        stages.setObjectName("ImportStages")
        stages_layout = QVBoxLayout(stages)
        stages_layout.setContentsMargins(0, 0, 0, 0)
        stages_layout.setSpacing(6)
        for stage in self._STAGES:
            row = QFrame()
            row.setObjectName("ImportStageRow")
            row.setProperty("stageState", "pending")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(10, 7, 10, 7)
            row_layout.setSpacing(9)
            mark = QLabel("○")
            mark.setObjectName("ImportStageMark")
            mark.setFixedWidth(20)
            label = QLabel(self._LABELS[stage])
            label.setObjectName("ImportStageLabel")
            self.stage_labels[stage] = label
            self.stage_marks[stage] = mark
            self.stage_rows[stage] = row
            row_layout.addWidget(mark)
            row_layout.addWidget(label, 1)
            stages_layout.addWidget(row)
        layout.addWidget(stages)
            
        self.progress = QProgressBar()
        self.progress.setObjectName("ImportProgressBar")
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)
        
        self.detail = QLabel()
        self.detail.setObjectName("ImportDetail")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        
        self.summary = QLabel()
        self.summary.setObjectName("ImportSummary")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        
        host = QHBoxLayout()
        host.addStretch()
        host.addWidget(card)
        host.addStretch()
        root.addLayout(host)
        root.addStretch()

    def begin(self, paths: list[Path]) -> None:
        name = WORKSPACE._default_project_name(paths)
        self.project_name.setText(name)
        self.summary.clear()
        self.update_progress("detecting", 0, 0, "Scanning folders…")

    def update_progress(self, stage: str, current: int, total: int, detail: str) -> None:
        active_index = self._STAGES.index(stage)
        for index, value in enumerate(self._STAGES):
            state = "complete" if index < active_index else ("active" if index == active_index else "pending")
            marker = "✓" if state == "complete" else ("●" if state == "active" else "○")
            self.stage_marks[value].setText(marker)
            self.stage_rows[value].setProperty("stageState", state)
            self.stage_rows[value].style().unpolish(self.stage_rows[value])
            self.stage_rows[value].style().polish(self.stage_rows[value])
            self.stage_labels[value].setProperty("active", state == "active")
            self.stage_labels[value].style().unpolish(self.stage_labels[value])
            self.stage_labels[value].style().polish(self.stage_labels[value])
            
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(current)
            self.progress.setFormat(f"{current} / {total}")
        else:
            self.progress.setRange(0, 0)
            self.progress.setFormat("")
            
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
