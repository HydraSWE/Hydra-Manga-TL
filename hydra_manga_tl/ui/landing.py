"""Landing and import-progress screens."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy,
    QSpacerItem, QVBoxLayout, QWidget
)

from hydra_manga_tl.core.assets import find_asset
from hydra_manga_tl.core.paths import PATHS
from hydra_manga_tl.core.settings import SETTINGS
from hydra_manga_tl.core.updater import STATUS_AVAILABLE, STATUS_CHECKING, STATUS_FAILED, UPDATER, UpdateState
from hydra_manga_tl.core.user_errors import workspace_action_error
from hydra_manga_tl.ui.shared import _landing_icon, _relative_opened_label, lucide_icon
from hydra_manga_tl.ui.landing_import_progress import ImportProgressScreen
from hydra_manga_tl.ui.landing_widgets import (
    DropZone,
    RECENT_PROJECT_CARD_HEIGHT,
    RECENT_PROJECT_CARD_WIDTH,
    RecentProjectCard,
    RecentProjectsScrollArea,
)
from hydra_manga_tl.project.workspace import WORKSPACE, RecentProjectSummary


LANDING_RECENT_VISIBLE_LIMIT = 5
LANDING_RECENT_SCROLL_PADDING = 24
RECENT_DIALOG_CARD_WIDTH = RECENT_PROJECT_CARD_WIDTH
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
        width = max(viewport_width, self.width() - 36)
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
        self.recent_scroll.setFixedHeight(RECENT_PROJECT_CARD_HEIGHT + LANDING_RECENT_SCROLL_PADDING)

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

        minimum_width = (
            len(summaries) * RECENT_PROJECT_CARD_WIDTH
            + max(0, len(summaries) - 1) * self.recent_layout.spacing()
            + self.recent_layout.contentsMargins().left()
            + self.recent_layout.contentsMargins().right()
        )
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
