"""Reusable widgets for the landing screen."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
)

from hydra_manga_tl.core.assets import find_asset
from hydra_manga_tl.project.workspace import RecentProjectSummary
from hydra_manga_tl.ui.shared import _landing_icon, _relative_opened_label, lucide_icon


RECENT_PROJECT_CARD_WIDTH = 342
RECENT_PROJECT_CARD_HEIGHT = 162


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
        self.setFixedSize(RECENT_PROJECT_CARD_WIDTH, RECENT_PROJECT_CARD_HEIGHT)

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
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(2)

        self.title_label = QLabel(summary.name)
        self.title_label.setObjectName("RecentProjectTitle")
        self.title_label.setMinimumWidth(0)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
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
        self.language_label.setMinimumWidth(0)
        self.language_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        page_word = "page" if summary.page_count == 1 else "pages"
        self.pages_label = QLabel(f"{summary.page_count} {page_word}")
        self.pages_label.setObjectName("RecentMetaChip")
        self.pages_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        state_text = summary.state_display or summary.state_label or "Not Started"
        self.state_label = QLabel(state_text)
        self.state_label.setObjectName("RecentStateLine")

        if summary.exported:
            export_type_display = {
                "folder": "Exported Folder",
                "archive": "Exported Archive",
                "pdf": "Exported PDF",
            }.get(summary.export_type, "Exported")
            parts = [export_type_display]
            if summary.export_count:
                page_label = "page" if summary.export_count == 1 else "pages"
                parts.append(f"{summary.export_count} {page_label}")
            if summary.export_relative_time:
                parts.append(summary.export_relative_time)
            export_text = " • ".join(parts)
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
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            label.setToolTip(label.text())
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
