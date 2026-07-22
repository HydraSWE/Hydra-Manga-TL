"""Two-screen landing and translation workspace."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QFont, QFontMetrics, QIcon, QPainter, QPainterPath, QPen, QPolygonF, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGraphicsPixmapItem, QGraphicsPolygonItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsView, QGridLayout, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem, QMainWindow,
    QLineEdit, QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QStackedWidget, QStyle, QTabWidget, QSizePolicy, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from .editor_project import RegionEdit
from .import_scan import ImportScanResult, ImportScanWorker, ThumbnailWorker
from .language import resolve_source_language
from .manual_region import normalize_image_rect
from .state import APP_STATE
from .settings import CREDENTIALS, SETTINGS
from .speech import SpeechService
from .translation_engines.model_manager import KNOWN_MODEL_PACKAGES
from .workspace import WORKSPACE, RecentProjectSummary


TARGET_LANGUAGE_NAMES = {"en": "English"}
FILMSTRIP_CARD_SIZE = QSize(88, 104)
FILMSTRIP_PREVIEW_SIZE = QSize(72, 78)


def _page_label(path: str, index: int) -> str:
    del path
    return str(index + 1)


def _language_badge(prefix: str, language: str) -> str:
    return f"{prefix} ({language})" if language else prefix


def _relative_opened_label(value: str, now: datetime | None = None) -> str:
    if not value:
        return "Last opened recently"
    try:
        opened = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "Last opened recently"
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    days = (current.astimezone().date() - opened.astimezone().date()).days
    if days == 0:
        return "Last opened Today"
    if days == 1:
        return "Last opened Yesterday"
    if 1 < days < 7:
        return f"Last opened {days} days ago"
    return f"Last opened {opened.astimezone().strftime('%b')} {opened.astimezone().day}"


def _landing_icon(kind: str, size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#4d83ff"), max(2.0, size / 18), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    if kind == "folder":
        path = QPainterPath()
        path.moveTo(size * .16, size * .32)
        path.lineTo(size * .16, size * .76)
        path.lineTo(size * .82, size * .76)
        path.lineTo(size * .88, size * .42)
        path.lineTo(size * .47, size * .42)
        path.lineTo(size * .39, size * .31)
        path.closeSubpath()
        painter.drawPath(path)
    else:
        painter.drawLine(int(size * .18), int(size * .25), int(size * .18), int(size * .78))
        painter.drawLine(int(size * .82), int(size * .25), int(size * .82), int(size * .78))
        painter.drawLine(int(size * .50), int(size * .32), int(size * .50), int(size * .82))
        left = QPainterPath(); left.moveTo(size * .18, size * .25); left.quadTo(size * .37, size * .20, size * .50, size * .32)
        right = QPainterPath(); right.moveTo(size * .82, size * .25); right.quadTo(size * .63, size * .20, size * .50, size * .32)
        painter.drawPath(left); painter.drawPath(right)
        painter.drawLine(int(size * .18), int(size * .78), int(size * .50), int(size * .82))
        painter.drawLine(int(size * .82), int(size * .78), int(size * .50), int(size * .82))
    painter.end()
    return pixmap


class ReorderableFilmstrip(QListWidget):
    """Horizontal page list that reports its stable ID order after a move."""

    order_changed = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self._reorder_enabled = True
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)
        self.setDragEnabled(True)
        self.viewport().setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

    def set_reorder_enabled(self, enabled: bool) -> None:
        self._reorder_enabled = enabled
        self.setDragEnabled(enabled)
        self.viewport().setAcceptDrops(enabled)
        self.setDropIndicatorShown(enabled)
        self.setToolTip(
            "Drag pages to reorder them. Ctrl/Shift-click to move several pages together."
            if enabled else "Page order cannot be changed while translation is running."
        )

    def dropEvent(self, event: QDropEvent) -> None:
        if not self._reorder_enabled or event.source() is not self:
            event.ignore()
            return
        position = event.position().toPoint()
        target_item = self.itemAt(position)
        if target_item is None:
            target_index = self.count()
        else:
            target_index = self.row(target_item)
            if position.x() >= self.visualItemRect(target_item).center().x():
                target_index += 1
        self._move_selected_to(target_index)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def _move_selected_to(self, target_index: int) -> bool:
        before = self.ordered_ids()
        selected = {str(item.data(Qt.ItemDataRole.UserRole)) for item in self.selectedItems()}
        selected_ids = [image_id for image_id in before if image_id in selected]
        if not selected_ids:
            return False
        current_item = self.currentItem()
        current_id = str(current_item.data(Qt.ItemDataRole.UserRole)) if current_item is not None else ""
        selected_positions = [index for index, image_id in enumerate(before) if image_id in selected]
        remaining = [image_id for image_id in before if image_id not in selected]
        adjusted_target = target_index - sum(index < target_index for index in selected_positions)
        adjusted_target = max(0, min(len(remaining), adjusted_target))
        after = remaining[:adjusted_target] + selected_ids + remaining[adjusted_target:]
        if after == before:
            return False

        items = {str(self.item(row).data(Qt.ItemDataRole.UserRole)): self.item(row) for row in range(self.count())}
        blocked = self.blockSignals(True)
        try:
            while self.count():
                self.takeItem(0)
            for image_id in after:
                self.addItem(items[image_id])
            if current_id in items:
                self.setCurrentItem(items[current_id])
            self.clearSelection()
            for image_id in selected_ids:
                items[image_id].setSelected(True)
        finally:
            self.blockSignals(blocked)
        self.order_changed.emit(after)
        return True

    def ordered_ids(self) -> list[str]:
        return [str(self.item(row).data(Qt.ItemDataRole.UserRole)) for row in range(self.count())]


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
        self._banner_source = QPixmap(str(self._asset_path("assets/logos/mainlogo.png")))
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

    @staticmethod
    def _asset_path(relative: str) -> Path:
        return Path(__file__).resolve().parent.parent / relative

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
        WORKSPACE.forget_recent_project(path)
        self.refresh_recent()

    def _confirm_clear_history(self) -> None:
        answer = QMessageBox.question(
            self,
            "Clear Recent Project History?",
            "Remove all recent-project shortcuts?\n\nYour project files will not be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            WORKSPACE.clear_recent_projects()
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


class CanvasView(QGraphicsView):
    region_selected = Signal(int)
    manual_rect_created = Signal(object)
    zoom_changed = Signal(float)

    def __init__(self, title: str) -> None:
        super().__init__()
        self.title = title
        self.setMinimumWidth(300)
        self._scene = QGraphicsScene(self); self.setScene(self._scene)
        self._pixmap = QGraphicsPixmapItem(); self._scene.addItem(self._pixmap)
        self._regions: list[QGraphicsPolygonItem] = []
        self._manual_mode = False
        self._manual_start: QPointF | None = None
        self._manual_preview: QGraphicsRectItem | None = None
        self._zoom = 1.0
        # Manga line art looks soft when Qt applies bilinear filtering while a
        # fitted image is slightly enlarged. Smooth only true downscaling;
        # preserve source pixels at 100% and above.
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.badge = QLabel(title, self.viewport())
        self.badge.setObjectName("CanvasBadge")
        self.badge.move(10, 10)
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.badge.adjustSize()
        self.badge.raise_()

    def set_badge(self, text: str) -> None:
        self.badge.setText(text)
        self.badge.adjustSize()
        self.badge.raise_()

    def set_content(self, image_path: Path | None, groups: list[dict], selected: int = -1) -> None:
        self.cancel_manual_selection()
        for item in self._regions: self._scene.removeItem(item)
        self._regions.clear()
        pixmap = QPixmap(str(image_path)) if image_path and image_path.is_file() else QPixmap()
        self._pixmap.setPixmap(pixmap); self._scene.setSceneRect(self._pixmap.boundingRect())
        self._update_pixmap_filter()
        for row, group in enumerate(groups):
            polygon = QPolygonF([QPointF(point[0], point[1]) for point in group.get("polygon", [])])
            item = QGraphicsPolygonItem(polygon)
            item.setData(0, row)
            active = row == selected
            manual = bool(group.get("manual"))
            inactive_color = "#b36cff" if manual else "#4d91ff"
            item.setPen(QPen(QColor("#ffd35a" if active else inactive_color), 3 if active else (2 if manual else 1)))
            item.setBrush(QColor(255, 211, 90, 28) if active else (QColor(179, 108, 255, 24) if manual else QColor(77, 145, 255, 20)))
            item.setZValue(2); self._scene.addItem(item); self._regions.append(item)
        if not pixmap.isNull() and self._zoom == 1.0: self.fit_image()

    def begin_manual_selection(self) -> bool:
        if self._pixmap.pixmap().isNull():
            return False
        self._manual_mode = True
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setCursor(Qt.CursorShape.CrossCursor)
        return True

    def cancel_manual_selection(self) -> None:
        self._manual_mode = False
        self._manual_start = None
        if self._manual_preview is not None:
            self._scene.removeItem(self._manual_preview)
            self._manual_preview = None
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.unsetCursor()

    def mousePressEvent(self, event) -> None:
        if self._manual_mode and event.button() == Qt.MouseButton.LeftButton:
            self._manual_start = self.mapToScene(event.position().toPoint())
            self._manual_preview = QGraphicsRectItem()
            self._manual_preview.setPen(QPen(QColor("#b36cff"), 2, Qt.PenStyle.DashLine))
            self._manual_preview.setBrush(QColor(179, 108, 255, 28))
            self._manual_preview.setZValue(5)
            self._scene.addItem(self._manual_preview)
            event.accept()
            return
        item = self.itemAt(event.position().toPoint())
        while item is not None:
            row = item.data(0)
            if row is not None:
                self.region_selected.emit(int(row)); break
            item = item.parentItem()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._manual_start is not None and self._manual_preview is not None:
            current = self.mapToScene(event.position().toPoint())
            rect = QRectF(self._manual_start, current).normalized().intersected(self._pixmap.boundingRect())
            self._manual_preview.setRect(rect)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._manual_start is not None and event.button() == Qt.MouseButton.LeftButton:
            end = self.mapToScene(event.position().toPoint())
            start = (self._manual_start.x(), self._manual_start.y())
            finish = (end.x(), end.y())
            size = (self._pixmap.pixmap().width(), self._pixmap.pixmap().height())
            rect = normalize_image_rect(start, finish, size)
            self.cancel_manual_selection()
            if rect is not None:
                self.manual_rect_created.emit(rect)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self._zoom = max(0.15, min(6.0, self._zoom * factor))
        self.setTransform(self.transform().fromScale(self._zoom, self._zoom))
        self._update_pixmap_filter()
        self.zoom_changed.emit(self._zoom)

    def set_zoom(self, zoom: float) -> None:
        self._zoom = zoom; self.resetTransform(); self.scale(zoom, zoom); self._update_pixmap_filter()

    def fit_image(self) -> None:
        if not self._pixmap.pixmap().isNull():
            self.fitInView(self._pixmap, Qt.AspectRatioMode.KeepAspectRatio)
            self._zoom = self.transform().m11(); self._update_pixmap_filter(); self.zoom_changed.emit(self._zoom)

    def actual_size(self) -> None: self.set_zoom(1.0)

    def _update_pixmap_filter(self) -> None:
        mode = (
            Qt.TransformationMode.SmoothTransformation
            if self.transform().m11() < 0.999
            else Qt.TransformationMode.FastTransformation
        )
        self._pixmap.setTransformationMode(mode)


class SettingsDialog(QDialog):
    """Local-first provider preferences with secrets stored outside settings JSON."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Hydra Settings")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.literal = QComboBox(); self.literal.addItem("MarianMT (Local)", "marian"); self.literal.addItem("Google Cloud Translation", "google")
        self.localization = QComboBox()
        for label, value in (("Local manga cleanup", "local"), ("Gemini", "gemini"), ("Groq", "groq"), ("DeepSeek", "deepseek")):
            self.localization.addItem(label, value)
        self.translation_engine = QComboBox()
        self.translation_engine.addItem("Marian fallback", "marian")
        self.translation_engine.addItem("Local Qwen (optional)", "qwen")
        self.qwen_model = QComboBox()
        for package in KNOWN_MODEL_PACKAGES.values():
            self.qwen_model.addItem(package.label, package.key)
        self.qwen_model_path = QLineEdit(SETTINGS.qwen_model_path)
        self.qwen_model_path.setPlaceholderText("Path to a .gguf model")
        self.qwen_status = QLabel(SETTINGS.qwen_model_status or "Not installed")
        self.qwen_estimate = QLabel("Estimated download: not available")
        self.qwen_browse = QPushButton("Browse")
        self.qwen_browse.clicked.connect(self._browse_qwen_model)
        self.qwen_download = QPushButton("Download Model")
        self.qwen_download.clicked.connect(self._download_qwen_model)
        self.qwen_test = QPushButton("Test translation")
        self.qwen_test.clicked.connect(self._test_qwen_translation)
        qwen_layout = QHBoxLayout(); qwen_layout.addWidget(self.qwen_model_path); qwen_layout.addWidget(self.qwen_browse)
        self.gemini_model = QLineEdit(SETTINGS.gemini_model)
        self.groq_model = QLineEdit(SETTINGS.groq_model)
        self.deepseek_model = QLineEdit(SETTINGS.deepseek_model)
        self.keys = {}
        for provider in ("google", "gemini", "groq", "deepseek"):
            field = QLineEdit(); field.setEchoMode(QLineEdit.EchoMode.Password)
            field.setPlaceholderText("Stored securely" if CREDENTIALS.get(provider) else "Not configured")
            self.keys[provider] = field
        form.addRow("Literal translation", self.literal); form.addRow("Manga localization", self.localization)
        form.addRow("Translation engine", self.translation_engine)
        form.addRow("Model", self.qwen_model)
        form.addRow("Qwen GGUF model", qwen_layout)
        form.addRow("Status", self.qwen_status)
        form.addRow("Estimated download", self.qwen_estimate)
        form.addRow("", self.qwen_download)
        form.addRow("", self.qwen_test)
        form.addRow("Gemini model", self.gemini_model); form.addRow("Gemini API key", self.keys["gemini"])
        form.addRow("Groq model", self.groq_model); form.addRow("Groq API key", self.keys["groq"])
        form.addRow("DeepSeek model", self.deepseek_model); form.addRow("DeepSeek API key", self.keys["deepseek"])
        form.addRow("Google Translate key", self.keys["google"])
        warning = QLabel("Cloud services are optional and may enforce quotas or charges. Hydra never switches to a cloud provider automatically.")
        warning.setWordWrap(True); warning.setObjectName("Muted")
        layout.addLayout(form); layout.addWidget(warning)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.literal.setCurrentIndex(max(0, self.literal.findData(SETTINGS.literal_provider)))
        self.localization.setCurrentIndex(max(0, self.localization.findData(SETTINGS.localization_provider)))
        self.translation_engine.setCurrentIndex(max(0, self.translation_engine.findData(SETTINGS.translation_engine)))
        model_index = max(0, self.qwen_model.findData(SETTINGS.qwen_model_name or "qwen3-4b"))
        self.qwen_model.setCurrentIndex(model_index)
        self._refresh_qwen_metadata()

    def _browse_qwen_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select Qwen GGUF model", "", "GGUF models (*.gguf);;All files (*.*)")
        if path:
            self.qwen_model_path.setText(path)
            self.qwen_status.setText("Installed" if Path(path).exists() else "Not installed")
            self._refresh_qwen_metadata()

    def _download_qwen_model(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save Qwen GGUF model", "", "GGUF models (*.gguf);;All files (*.*)")
        if path:
            self.qwen_model_path.setText(path)
            self.qwen_status.setText("Ready to download")
            self._refresh_qwen_metadata()

    def _test_qwen_translation(self) -> None:
        from .translation_engines import PageDialogue, TranslationEngineManager
        page = PageDialogue(source_language="Japanese", target_language="en", dialogue=[{"id": "r1", "text": "待て！"}])
        manager = TranslationEngineManager(
            glossary={},
            qwen_model_path=self.qwen_model_path.text().strip() or None,
            preferred_engine=self.translation_engine.currentData() or "qwen",
            qwen_model_name=self.qwen_model.currentData() or "qwen3-4b",
        )
        try:
            manager.load()
            result = manager.translate_page(page)
            sample = result.translations[0]["text"] if result.translations else ""
            QMessageBox.information(self, "Test translation", sample or "The engine returned an empty translation.")
        except Exception as error:
            QMessageBox.warning(self, "Test translation failed", str(error))
        finally:
            manager.unload()

    def _refresh_qwen_metadata(self) -> None:
        package = KNOWN_MODEL_PACKAGES.get(self.qwen_model.currentData() or "qwen3-4b")
        if package is None:
            self.qwen_estimate.setText("Estimated download: not available")
            return
        self.qwen_estimate.setText(f"{package.label} · {package.quantization} · {package.estimated_download} · {package.recommended_for}")

    def _save(self) -> None:
        try:
            for provider, field in self.keys.items():
                if field.text().strip():
                    CREDENTIALS.set(provider, field.text())
        except RuntimeError as error:
            QMessageBox.warning(self, "Could not save API key", str(error)); return
        SETTINGS.literal_provider = self.literal.currentData()
        SETTINGS.localization_provider = self.localization.currentData()
        SETTINGS.translation_engine = self.translation_engine.currentData() or "qwen"
        SETTINGS.qwen_model_path = self.qwen_model_path.text().strip()
        SETTINGS.qwen_model_name = self.qwen_model.currentData() or "qwen3-4b"
        SETTINGS.qwen_model_status = self.qwen_status.text().strip() or "Not installed"
        SETTINGS.gemini_model = self.gemini_model.text().strip() or "gemini-3.5-flash"
        SETTINGS.groq_model = self.groq_model.text().strip() or "qwen/qwen3-32b"
        SETTINGS.deepseek_model = self.deepseek_model.text().strip() or "deepseek-v4-flash"
        SETTINGS.save()
        if WORKSPACE.current is not None:
            WORKSPACE.current.literal_provider = SETTINGS.literal_provider
            WORKSPACE.current.localization_provider = SETTINGS.localization_provider
            WORKSPACE.current.localization_model = SETTINGS.model_for(SETTINGS.localization_provider)
            WORKSPACE.save()
        self.accept()


class GlossaryDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent); self.setWindowTitle("Project Glossary"); self.setMinimumSize(460, 340)
        layout = QVBoxLayout(self)
        help_text = QLabel("Enter one protected name or term per line as source = English. These spellings are reused throughout this project.")
        help_text.setWordWrap(True); layout.addWidget(help_text)
        self.values = QTextEdit()
        glossary = WORKSPACE.current.glossary if WORKSPACE.current else {}
        self.values.setPlainText("\n".join(f"{source} = {target}" for source, target in glossary.items()))
        layout.addWidget(self.values)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def _save(self) -> None:
        if WORKSPACE.current is None:
            self.reject(); return
        glossary = {}
        for number, raw in enumerate(self.values.toPlainText().splitlines(), 1):
            if not raw.strip():
                continue
            if "=" not in raw:
                QMessageBox.warning(self, "Invalid glossary", f"Line {number} must use source = English."); return
            source, target = [value.strip() for value in raw.split("=", 1)]
            if not source or not target:
                QMessageBox.warning(self, "Invalid glossary", f"Line {number} has an empty source or translation."); return
            glossary[source] = target
        WORKSPACE.current.glossary = glossary; WORKSPACE.save(); self.accept()


class WorkspaceScreen(QWidget):
    close_requested = Signal()

    _PROGRESS_RANGES = {
        "analyzing": (0.0, 5.0),
        "ocr": (5.0, 30.0),
        "translating": (30.0, 50.0),
        "reconstructing": (50.0, 98.0),
    }

    def __init__(self) -> None:
        super().__init__()
        self._syncing = False
        self._groups: list[dict] = []
        self._project_title_full = "Project"
        self._filmstrip_project_id = ""
        self._filmstrip_items: dict[str, QListWidgetItem] = {}
        self._thumbnail_jobs: list[tuple[QThread, ThumbnailWorker]] = []
        self._page_progress_value = 0.0
        self._page_progress_ceiling = 0.0
        self._job_position = 0
        self._job_total = 0
        self._completed_pages = 0
        self._active_page_in_overall = False
        self._progress_stage = ""
        self._job_failure_count = 0
        self._current_job_filename = ""
        self._job_panel_expanded = False
        self._job_manually_collapsed = False
        self._has_job_details = False
        self._terminal_job_state = ""
        self._job_is_busy = False
        self.speech = SpeechService(self)
        self.speech.unavailable.connect(lambda message: QMessageBox.information(self, "Original text voice", message))
        self._build()
        self._progress_timer = QTimer(self); self._progress_timer.setInterval(100)
        self._progress_timer.timeout.connect(self._advance_progress_animation)
        self._job_collapse_timer = QTimer(self); self._job_collapse_timer.setSingleShot(True); self._job_collapse_timer.setInterval(3000)
        self._job_collapse_timer.timeout.connect(self._auto_collapse_job_panel)
        APP_STATE.project_changed.connect(self.refresh)
        APP_STATE.selection_changed.connect(self._on_selection)
        APP_STATE.pipeline_changed.connect(self._on_pipeline)
        APP_STATE.busy_changed.connect(self._on_busy)
        WORKSPACE.image_updated.connect(lambda _: self.refresh(APP_STATE.project))
        WORKSPACE.manual_region_finished.connect(self._on_manual_region_finished)
        WORKSPACE.manual_region_failed.connect(self._on_manual_region_failed)
        WORKSPACE.manual_region_busy_changed.connect(self._on_manual_region_busy)

    def _build(self) -> None:
        root = QVBoxLayout(self); root.setContentsMargins(12, 10, 12, 8); root.setSpacing(8)
        header = QFrame(); header.setObjectName("Header")
        row = QHBoxLayout(header)
        self.project_title = QLabel("Project"); self.project_title.setObjectName("Heading")
        self.count_label = QLabel("0 images"); self.count_label.setObjectName("Muted")
        self.source_combo = QComboBox()
        for label, value in (("Auto Detect", "auto"), ("Japanese", "Japanese"), ("Chinese", "Chinese"), ("English", "Latin-script")):
            self.source_combo.addItem(label, value)
        self.source_combo.currentIndexChanged.connect(self._set_source_language)
        self.target_combo = QComboBox(); self.target_combo.addItem("English", "en")
        self.quality_combo = QComboBox(); self.quality_combo.addItems(["Fast", "Balanced", "Maximum"]); self.quality_combo.setCurrentText("Balanced")
        self.quality_combo.currentTextChanged.connect(self._set_quality)
        self.style_combo = QComboBox(); self.style_combo.addItems(["Manga", "Comic", "Novel"]); self.style_combo.currentTextChanged.connect(self._set_text_style)
        self.start_button = QPushButton("Translate All Pending"); self.start_button.setObjectName("Primary"); self.start_button.clicked.connect(lambda: WORKSPACE.start_pipeline())
        self.selected_button = QPushButton("Translate Selected"); self.selected_button.clicked.connect(self._translate_selected_from_button)
        self.cancel_button = QPushButton("Cancel"); self.cancel_button.clicked.connect(WORKSPACE.cancel_pipeline); self.cancel_button.setEnabled(False)
        save = QPushButton("Save"); save.clicked.connect(WORKSPACE.save)
        export = QPushButton("Export"); export.clicked.connect(self._export)
        self.close_button = QPushButton("Close"); self.close_button.clicked.connect(self.close_requested)
        settings = QPushButton("Settings"); settings.clicked.connect(self._open_settings)
        glossary = QPushButton("Glossary"); glossary.clicked.connect(lambda: GlossaryDialog(self).exec())
        for widget in (self.project_title, self.count_label): row.addWidget(widget)
        row.addStretch()
        for widget in (self.source_combo, self.target_combo, self.quality_combo, self.style_combo, self.selected_button, self.start_button, self.cancel_button, glossary, settings, save, export, self.close_button): row.addWidget(widget)
        root.addWidget(header)

        tools = QHBoxLayout()
        previous = QPushButton("‹"); previous.clicked.connect(lambda: self._move_image(-1))
        next_button = QPushButton("›"); next_button.clicked.connect(lambda: self._move_image(1))
        fit = QPushButton("Fit"); fit.clicked.connect(self._fit_both)
        actual = QPushButton("100%"); actual.clicked.connect(self._actual_both)
        self.add_box = QPushButton("Add Text Box"); self.add_box.clicked.connect(self._begin_manual_box)
        self.image_label = QLabel("No image")
        self.selection_label = QLabel("1 selected"); self.selection_label.setObjectName("Muted")
        tools.addWidget(previous); tools.addWidget(next_button); tools.addWidget(self.image_label); tools.addWidget(self.selection_label); tools.addStretch(); tools.addWidget(self.add_box); tools.addWidget(fit); tools.addWidget(actual)
        root.addLayout(tools)

        main = QSplitter(Qt.Orientation.Horizontal); self.main_splitter = main
        main.setChildrenCollapsible(False)
        canvas_host = QWidget(); canvas_layout = QVBoxLayout(canvas_host); canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvases = QSplitter(Qt.Orientation.Horizontal)
        self.original = CanvasView("Original"); self.translated = CanvasView("Translated")
        canvases.addWidget(self.original); canvases.addWidget(self.translated); canvases.setSizes([600, 600]); canvas_layout.addWidget(canvases)
        self.filmstrip = ReorderableFilmstrip(); self.filmstrip.setObjectName("Filmstrip")
        self.filmstrip.setViewMode(QListView.ViewMode.IconMode); self.filmstrip.setFlow(QListView.Flow.LeftToRight)
        self.filmstrip.setResizeMode(QListView.ResizeMode.Adjust); self.filmstrip.setMovement(QListView.Movement.Snap)
        self.filmstrip.setWrapping(False); self.filmstrip.setUniformItemSizes(True); self.filmstrip.setSpacing(5)
        self.filmstrip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.filmstrip.setIconSize(FILMSTRIP_PREVIEW_SIZE); self.filmstrip.setMaximumHeight(124); self.filmstrip.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.filmstrip.set_reorder_enabled(True); self.filmstrip.order_changed.connect(self._on_filmstrip_reordered)
        self.filmstrip.currentRowChanged.connect(self._filmstrip_current_changed)
        self.filmstrip.itemSelectionChanged.connect(self._selection_changed)
        self.filmstrip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.filmstrip.customContextMenuRequested.connect(self._filmstrip_menu)
        canvas_layout.addWidget(self.filmstrip)
        main.addWidget(canvas_host)
        self.inspector = self._build_inspector(); main.addWidget(self.inspector)
        main.setStretchFactor(0, 1); main.setStretchFactor(1, 0)
        self.inspector.setMinimumWidth(420); main.setSizes([1000, 500])
        root.addWidget(main, 1)
        self.job_panel = QFrame(); self.job_panel.setObjectName("ProgressPanel")
        job_layout = QVBoxLayout(self.job_panel); job_layout.setContentsMargins(10, 5, 10, 5); job_layout.setSpacing(4)
        job_header = QHBoxLayout(); job_header.setSpacing(6)
        self.job_toggle = QToolButton(); self.job_toggle.setObjectName("JobToggle"); self.job_toggle.setArrowType(Qt.ArrowType.RightArrow); self.job_toggle.setAutoRaise(True); self.job_toggle.setEnabled(False)
        self.job_toggle.setToolTip("Show translation job details"); self.job_toggle.clicked.connect(self._toggle_job_panel)
        job_title = QLabel("Translation Job"); job_title.setObjectName("JobTitle")
        self.job_overall = QLabel("Idle"); self.job_overall.setObjectName("Muted")
        job_header.addWidget(self.job_toggle); job_header.addWidget(job_title); job_header.addStretch(); job_header.addWidget(self.job_overall); job_layout.addLayout(job_header)
        self.job_body = QWidget(); body_layout = QVBoxLayout(self.job_body); body_layout.setContentsMargins(14, 2, 0, 1); body_layout.setSpacing(4)
        self.status = QLabel("Ready"); self.status.setObjectName("Muted"); body_layout.addWidget(self.status)
        overall_row = QHBoxLayout(); overall_label = QLabel("Overall"); overall_label.setObjectName("Muted")
        self.progress = QProgressBar(); self.progress.setRange(0, 1000); self.progress.setFormat("0.0%"); self.progress.hide()
        overall_row.addWidget(overall_label); overall_row.addWidget(self.progress, 1); body_layout.addLayout(overall_row)
        page_row = QHBoxLayout(); self.current_page_label = QLabel("Current page"); self.current_page_label.setObjectName("Muted")
        self.page_progress = QProgressBar(); self.page_progress.setRange(0, 1000); self.page_progress.setFormat("0.0%"); self.page_progress.hide()
        page_row.addWidget(self.current_page_label); page_row.addWidget(self.page_progress, 1); body_layout.addLayout(page_row)
        self.stage_status = QLabel(self._stage_text("")); self.stage_status.setObjectName("PipelineStages"); body_layout.addWidget(self.stage_status)
        self.job_body.hide(); job_layout.addWidget(self.job_body)
        root.addWidget(self.job_panel)

        self.original.region_selected.connect(self._select_block); self.translated.region_selected.connect(self._select_block)
        self.original.manual_rect_created.connect(self._manual_rect_created)
        self.original.zoom_changed.connect(self.translated.set_zoom); self.translated.zoom_changed.connect(self.original.set_zoom)
        self._sync_scrollbars(self.original, self.translated); self._sync_scrollbars(self.translated, self.original)

    def _build_inspector(self) -> QFrame:
        frame = QFrame(); frame.setObjectName("Inspector"); layout = QVBoxLayout(frame)
        tabs = QTabWidget(); layout.addWidget(tabs)
        text_tab = QWidget(); text_layout = QVBoxLayout(text_tab)
        self.blocks = QListWidget(); self.blocks.setWordWrap(True); self.blocks.setTextElideMode(Qt.TextElideMode.ElideRight); self.blocks.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); self.blocks.setMinimumHeight(125); self.blocks.currentRowChanged.connect(self._select_block)
        text_layout.addWidget(self.blocks, 1)
        form_host = QWidget(); form = QFormLayout(form_host); form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.original_text = QTextEdit(); self.original_text.setReadOnly(True); self.original_text.setMaximumHeight(58)
        self.original_text.setMinimumWidth(0); self.original_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        original_host = QWidget(); original_host.setMinimumWidth(0); original_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        original_row = QHBoxLayout(original_host); original_row.setContentsMargins(0, 0, 0, 0); original_row.setSpacing(6)
        self.speak_original = QToolButton(); self.speak_original.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume))
        self.speak_original.setToolTip("Play or stop the original text")
        self.speak_original.setFixedSize(30, 30)
        self.speak_original.setEnabled(False); self.speak_original.clicked.connect(self._speak_original)
        original_row.addWidget(self.original_text, 1); original_row.addWidget(self.speak_original)
        self.translation = QTextEdit(); self.translation.setMaximumHeight(72)
        self.confidence = QLabel("—"); self.replace = QCheckBox("Replace source text"); self.replace.setChecked(True)
        self.font = QComboBox()
        font_options = [
            ("Arial", QFont("Arial")), ("Arial Bold", QFont("Arial", weight=QFont.Weight.Bold)),
            ("Comic Sans MS", QFont("Comic Sans MS")), ("Segoe UI", QFont("Segoe UI")),
        ]
        for label, sample_font in font_options:
            self.font.addItem(label); self.font.setItemData(self.font.count() - 1, sample_font, Qt.ItemDataRole.FontRole)
        self.font.currentTextChanged.connect(self._update_font_preview)
        self.font_size = QSpinBox(); self.font_size.setRange(0, 120); self.font_size.setSpecialValueText("Auto")
        self.alignment = QComboBox()
        for label, value in (("Left", "left"), ("Center", "center"), ("Right", "right")): self.alignment.addItem(label, value)
        self.alignment.setCurrentIndex(self.alignment.findData("center"))
        self.color = QPushButton("#111111"); self.color.clicked.connect(self._choose_color)
        self.offset_x = QSpinBox(); self.offset_x.setRange(-500, 500); self.offset_x.setSuffix(" px")
        self.offset_y = QSpinBox(); self.offset_y.setRange(-500, 500); self.offset_y.setSuffix(" px")
        self.offset_x.setSingleStep(5); self.offset_y.setSingleStep(5)
        x_host = self._build_offset_control(self.offset_x, "left", "right")
        y_host = self._build_offset_control(self.offset_y, "up", "down")
        for label, widget in (("Original", original_host), ("Translation", self.translation), ("Confidence", self.confidence)): form.addRow(label, widget)
        form.addRow(self.replace)
        for label, widget in (("Font", self.font), ("Size", self.font_size), ("Color", self.color), ("Alignment", self.alignment), ("X", x_host), ("Y", y_host)): form.addRow(label, widget)
        form_scroll = QScrollArea(); form_scroll.setWidgetResizable(True); form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); form_scroll.setWidget(form_host)
        form_scroll.setMinimumHeight(245); form_scroll.setMaximumHeight(440)
        text_layout.addWidget(form_scroll)
        actions = QGridLayout(); actions.setHorizontalSpacing(6); actions.setVerticalSpacing(6)
        self.remove_block = QPushButton("Remove Block"); self.remove_block.clicked.connect(self._remove_selected_block); self.remove_block.setEnabled(False)
        self.remove_block.setToolTip("Remove the selected automatic block, or delete the selected manual block")
        self.restore_auto = QPushButton("Restore Auto"); self.restore_auto.clicked.connect(self._restore_auto_blocks); self.restore_auto.setEnabled(False)
        self.restore_auto.setToolTip("Restore automatic blocks that were removed from this page")
        self.apply_button = QPushButton("Apply && Rerender"); self.apply_button.setObjectName("Primary"); self.apply_button.clicked.connect(self._apply)
        self.apply_button.setToolTip("Save these text settings and rebuild the translated page")
        self.reset_button = QPushButton("Reset"); self.reset_button.clicked.connect(self._reset_edit)
        self.reset_button.setToolTip("Reset this block to its automatic text settings")
        actions.addWidget(self.remove_block, 0, 0); actions.addWidget(self.restore_auto, 0, 1)
        actions.addWidget(self.reset_button, 1, 0, 1, 2); actions.addWidget(self.apply_button, 2, 0, 1, 2)
        text_layout.addLayout(actions); tabs.addTab(text_tab, "Text Blocks")
        self._update_font_preview(self.font.currentText()); self._update_color_swatch("#111111")
        info_tab = QWidget(); info_layout = QFormLayout(info_tab)
        self.info_path = QLabel("—"); self.info_path.setWordWrap(True); self.info_language = QLabel("—"); self.info_status = QLabel("—")
        info_layout.addRow("Source", self.info_path); info_layout.addRow("Language", self.info_language); info_layout.addRow("Status", self.info_status); tabs.addTab(info_tab, "Image Info")
        return frame

    def _build_offset_control(self, spinbox: QSpinBox, negative_label: str, positive_label: str) -> QWidget:
        host = QWidget()
        layout = QHBoxLayout(host); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(4)
        negative = QPushButton("-"); negative.setFixedWidth(30); negative.setToolTip(f"Nudge {negative_label}")
        positive = QPushButton("+"); positive.setFixedWidth(30); positive.setToolTip(f"Nudge {positive_label}")
        negative.clicked.connect(lambda: spinbox.setValue(spinbox.value() - spinbox.singleStep()))
        positive.clicked.connect(lambda: spinbox.setValue(spinbox.value() + spinbox.singleStep()))
        layout.addWidget(negative); layout.addWidget(spinbox, 1); layout.addWidget(positive)
        return host

    def refresh(self, project) -> None:
        if project is None: return
        self._project_title_full = project.name; self.project_title.setToolTip(project.name); self._update_project_title()
        self.count_label.setText(f"{len(project.images)} images")
        self.start_button.setEnabled(not APP_STATE.busy and any(image.status in {"queued", "partial", "failed", "cancelled"} for image in project.images))
        self.quality_combo.setCurrentText(project.quality)
        self.style_combo.setCurrentText(project.text_style)
        source_index = self.source_combo.findData(project.source_language); self.source_combo.setCurrentIndex(max(0, source_index))
        current = max(0, min(APP_STATE.selected_image, len(project.images) - 1)) if project.images else -1
        image_ids = [self._image_id(image) for image in project.images]
        project_id = str(getattr(project, "id", project.name))
        if project_id != self._filmstrip_project_id or image_ids != list(self._filmstrip_items):
            self._rebuild_filmstrip(project, project_id, image_ids, current)
        else:
            for image_index, image in enumerate(project.images):
                self._update_filmstrip_item(self._filmstrip_items[image_ids[image_index]], image, image_index)
            if current >= 0 and self.filmstrip.currentRow() < 0:
                self.filmstrip.setCurrentRow(current)
        if current >= 0: self._load_image(current, APP_STATE.selected_block)

    @staticmethod
    def _image_id(image) -> str:
        return str(getattr(image, "id", image.source_path))

    def _rebuild_filmstrip(self, project, project_id: str, image_ids: list[str], current: int) -> None:
        selected_ids = {
            str(item.data(Qt.ItemDataRole.UserRole)) for item in self.filmstrip.selectedItems()
        } if project_id == self._filmstrip_project_id else set()
        self.filmstrip.blockSignals(True); self.filmstrip.clear(); self._filmstrip_items = {}
        thumbnail_inputs: list[tuple[str, str]] = []
        for image_index, image in enumerate(project.images):
            image_id = image_ids[image_index]
            item = QListWidgetItem(); item.setData(Qt.ItemDataRole.UserRole, image_id)
            item.setSizeHint(FILMSTRIP_CARD_SIZE); item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setIcon(self._thumbnail_icon())
            self._update_filmstrip_item(item, image, image_index)
            self.filmstrip.addItem(item); self._filmstrip_items[image_id] = item
            if Path(image.source_path).is_file():
                thumbnail_inputs.append((image_id, image.source_path))
        self._filmstrip_project_id = project_id
        if current >= 0:
            self.filmstrip.setCurrentRow(current)
        for image_id in selected_ids:
            if image_id in self._filmstrip_items:
                self._filmstrip_items[image_id].setSelected(True)
        self.filmstrip.blockSignals(False); self._selection_changed()
        if thumbnail_inputs:
            self._start_thumbnail_loading(project_id, thumbnail_inputs)

    @staticmethod
    def _update_filmstrip_item(item: QListWidgetItem, image, image_index: int) -> None:
        item.setText(_page_label(image.source_path, image_index))
        item.setForeground(QColor({"ready":"#66d69a", "review":"#ffcc66", "failed":"#ff6b73", "ocr":"#69a0ff", "translating":"#69a0ff", "reconstructing":"#69a0ff"}.get(image.status, "#d7deea")))
        details = f"{Path(image.source_path).name}\n{image.source_path}\nStatus: {image.status}"
        if image.error: details += f"\n{image.error}"
        item.setToolTip(details)

    def _start_thumbnail_loading(self, project_id: str, images: list[tuple[str, str]]) -> None:
        thread = QThread(self); worker = ThumbnailWorker(images, QSize(72, 78)); worker.moveToThread(thread)
        job = (thread, worker); self._thumbnail_jobs.append(job)
        thread.started.connect(worker.run)
        worker.thumbnail_ready.connect(lambda image_id, image, pid=project_id: self._apply_thumbnail(pid, image_id, image))
        worker.progress.connect(lambda current, total, name, pid=project_id: self._thumbnail_progress(pid, current, total, name))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda job=job: self._thumbnail_finished(job))
        thread.start()

    def _apply_thumbnail(self, project_id: str, image_id: str, image) -> None:
        if project_id != self._filmstrip_project_id or image_id not in self._filmstrip_items:
            return
        self._filmstrip_items[image_id].setIcon(self._thumbnail_icon(image))

    @staticmethod
    def _thumbnail_icon(image=None) -> QIcon:
        canvas = QPixmap(FILMSTRIP_PREVIEW_SIZE)
        canvas.fill(QColor("#0b1017"))
        painter = QPainter(canvas); painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setPen(QPen(QColor("#2a3749"), 1)); painter.drawRect(0, 0, canvas.width() - 1, canvas.height() - 1)
        if image is not None and not image.isNull():
            source = QPixmap.fromImage(image)
            target = source.scaled(
                canvas.width() - 4, canvas.height() - 4,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            )
            x = (canvas.width() - target.width()) // 2; y = (canvas.height() - target.height()) // 2
            painter.drawPixmap(x, y, target)
        painter.end()
        return QIcon(canvas)

    def _thumbnail_progress(self, project_id: str, current: int, total: int, name: str) -> None:
        if project_id == self._filmstrip_project_id and not APP_STATE.busy:
            count = len(WORKSPACE.current.images) if WORKSPACE.current is not None else total
            self.count_label.setText(f"{count} images • previews {current}/{total}")

    def _thumbnail_finished(self, job: tuple[QThread, ThumbnailWorker]) -> None:
        if job in self._thumbnail_jobs:
            self._thumbnail_jobs.remove(job)
        job[0].deleteLater()
        if not self._thumbnail_jobs and not APP_STATE.busy:
            count = len(WORKSPACE.current.images) if WORKSPACE.current is not None else 0
            self.count_label.setText(f"{count} images")

    def stop_thumbnail_loading(self) -> None:
        for thread, _ in list(self._thumbnail_jobs):
            thread.requestInterruption(); thread.quit(); thread.wait(3000)

    def _filmstrip_current_changed(self, row: int) -> None:
        if row >= 0:
            APP_STATE.select(row)

    def _on_filmstrip_reordered(self, ordered_ids: list[str]) -> None:
        selected_ids = {
            str(item.data(Qt.ItemDataRole.UserRole)) for item in self.filmstrip.selectedItems()
        }
        self._filmstrip_items = {
            str(self.filmstrip.item(row).data(Qt.ItemDataRole.UserRole)): self.filmstrip.item(row)
            for row in range(self.filmstrip.count())
        }
        if not WORKSPACE.reorder_images(ordered_ids):
            self._filmstrip_project_id = ""
            if WORKSPACE.current is not None:
                self.refresh(WORKSPACE.current)
            return
        self.filmstrip.blockSignals(True); self.filmstrip.clearSelection()
        for image_id in selected_ids:
            if image_id in self._filmstrip_items:
                self._filmstrip_items[image_id].setSelected(True)
        self.filmstrip.blockSignals(False); self._selection_changed()

    def _selection_changed(self) -> None:
        count = len(self.filmstrip.selectedItems())
        self.selection_label.setText(f"{count} selected")
        eligible = self._selected_image_ids(eligible_only=True)
        self.selected_button.setText(f"Translate Selected ({len(eligible)})" if eligible else "Translate Selected")
        self.selected_button.setEnabled(bool(eligible) and not APP_STATE.busy)

    def _filmstrip_menu(self, position) -> None:
        item = self.filmstrip.itemAt(position)
        if item is None:
            return
        if not item.isSelected():
            self.filmstrip.clearSelection(); item.setSelected(True); self.filmstrip.setCurrentItem(item)
        selected_ids = self._selected_image_ids(eligible_only=True)
        menu = QMenu(self)
        translate = menu.addAction(f"Translate Selected ({len(selected_ids)})")
        translate.setEnabled(bool(selected_ids) and not APP_STATE.busy)
        translate.triggered.connect(lambda: self._translate_selected(selected_ids))
        all_selected = self._selected_image_ids(eligible_only=False)
        completed = {
            image.id for image in WORKSPACE.current.images
            if image.id in all_selected and image.status in {"ready", "review"}
        } if WORKSPACE.current else set()
        retranslate = menu.addAction(f"Retranslate Completed ({len(completed)})")
        retranslate.setEnabled(bool(completed) and not APP_STATE.busy)
        retranslate.triggered.connect(lambda: self._retranslate_selected(completed))
        menu.exec(self.filmstrip.viewport().mapToGlobal(position))

    def _selected_image_ids(self, eligible_only: bool = False) -> set[str]:
        selected = {str(item.data(Qt.ItemDataRole.UserRole)) for item in self.filmstrip.selectedItems()}
        if not eligible_only or WORKSPACE.current is None:
            return selected
        return {
            image.id for image in WORKSPACE.current.images
            if image.id in selected and image.status in {"queued", "partial", "failed", "cancelled"}
        }

    @staticmethod
    def _translate_selected(image_ids: set[str]) -> None:
        if image_ids:
            WORKSPACE.start_pipeline(image_ids)

    def _translate_selected_from_button(self) -> None:
        self._translate_selected(self._selected_image_ids(eligible_only=True))

    def _retranslate_selected(self, image_ids: set[str]) -> None:
        if not image_ids:
            return
        answer = QMessageBox.question(
            self, "Retranslate completed pages",
            "Refresh automatic OCR and translations for the selected completed pages? Manual boxes and edits will be kept.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            WORKSPACE.start_pipeline(image_ids, retranslate=True)

    def _load_image(self, index: int, block: int = -1) -> None:
        project = WORKSPACE.current
        if project is None or not (0 <= index < len(project.images)): return
        image = project.images[index]; self.image_label.setText(f"Page {index + 1} of {len(project.images)}")
        self.info_path.setText(image.source_path); self.info_language.setText(image.source_language or "Not analyzed"); self.info_status.setText(image.status)
        self.original.set_badge(_language_badge("Original", image.source_language))
        target_name = TARGET_LANGUAGE_NAMES.get(project.target_language, project.target_language.upper())
        self.translated.set_badge(_language_badge("Translated", target_name))
        self._groups = []
        self.speech.stop(); self.speak_original.setEnabled(False)
        self.remove_block.setEnabled(False)
        self.restore_auto.setEnabled(bool(image.suppressed_auto_group_indices))
        if (image.translation_result and Path(image.translation_result).is_file()) or image.manual_regions:
            try: self._groups = WORKSPACE.effective_translation_payload(index)["translation_groups"]
            except (OSError, ValueError, json.JSONDecodeError): self._groups = []
        self.blocks.blockSignals(True); self.blocks.clear()
        for group in self._groups:
            badge = "[Manual] " if group.get("manual") else ""
            item = QListWidgetItem(f"{badge}{group['index']}. {group['translated_text']}")
            item.setToolTip(group["original_text"]); self.blocks.addItem(item)
        self.blocks.setCurrentRow(block); self.blocks.blockSignals(False)
        final = Path(image.rendered_image) if image.rendered_image else None
        self.original.set_content(Path(image.source_path), self._groups, block)
        self.translated.set_content(final, self._groups, block)
        if block >= 0: self._load_block(block)

    def _load_block(self, row: int) -> None:
        if not (0 <= row < len(self._groups)): return
        group = self._groups[row]; image = WORKSPACE.current.images[APP_STATE.selected_image]
        edit = image.edits.get(str(group["index"]), RegionEdit())
        self.original_text.setPlainText(group["original_text"]); self.translation.setPlainText(group["translated_text"])
        quality = group.get("translation_quality", "review" if group.get("review_reasons") else "good")
        reasons = ", ".join(group.get("review_reasons", []))
        self.confidence.setText(f"OCR {group['ocr_confidence']:.1%} • Translation {quality}" + (f" • {reasons}" if reasons else ""))
        self.speak_original.setEnabled(bool(group.get("original_text")))
        self.remove_block.setEnabled(True)
        self.remove_block.setText("Delete Manual" if group.get("manual") else "Remove Auto")
        self.replace.setChecked(edit.replace); self.font.setCurrentText(edit.font_family); self.font_size.setValue(edit.font_size)
        self._update_color_swatch(edit.color)
        alignment_index = self.alignment.findData(edit.alignment); self.alignment.setCurrentIndex(max(0, alignment_index))
        self.offset_x.setValue(edit.offset_x); self.offset_y.setValue(edit.offset_y)

    def _select_block(self, row: int) -> None:
        if row < 0: return
        APP_STATE.select(APP_STATE.selected_image, row); self.blocks.blockSignals(True); self.blocks.setCurrentRow(row); self.blocks.blockSignals(False)

    def _on_selection(self, image: int, block: int) -> None:
        if image >= 0:
            if WORKSPACE.current is not None and WORKSPACE.current.selected_image != image:
                WORKSPACE.current.selected_image = image; WORKSPACE.save()
            self.filmstrip.blockSignals(True); self.filmstrip.setCurrentRow(image); self.filmstrip.blockSignals(False); self._load_image(image, block)

    def _set_quality(self, quality: str) -> None:
        if WORKSPACE.current is not None and WORKSPACE.current.quality != quality:
            WORKSPACE.current.quality = quality; WORKSPACE.save()

    def _set_source_language(self) -> None:
        if WORKSPACE.current is not None:
            WORKSPACE.current.source_language = self.source_combo.currentData(); WORKSPACE.save()

    def _set_text_style(self, style: str) -> None:
        if WORKSPACE.current is None or WORKSPACE.current.text_style == style:
            return
        WORKSPACE.current.text_style = style
        WORKSPACE.current.localization_style = style
        WORKSPACE.current.max_lines = 5 if style == "Novel" else 3
        WORKSPACE.save()

    def _apply(self) -> None:
        row = APP_STATE.selected_block
        if row < 0 or row >= len(self._groups): return
        group = self._groups[row]
        edit = RegionEdit(self.translation.toPlainText(), self.replace.isChecked(), self.font_size.value(), self.offset_x.value(), self.offset_y.value(), self.font.currentText(), self.color.text(), self.alignment.currentData())
        try:
            WORKSPACE.validate_edit(APP_STATE.selected_image, group["index"], edit)
            WORKSPACE.update_edit(APP_STATE.selected_image, group["index"], edit)
            WORKSPACE.rerender_image(APP_STATE.selected_image)
            self._load_image(APP_STATE.selected_image, row)
            self.status.setText("Text style applied and page rerendered")
        except (OSError, ValueError) as error: QMessageBox.warning(self, "Could not render", str(error))

    def _reset_edit(self) -> None:
            project = WORKSPACE.current
            row = APP_STATE.selected_block
            
            # Add bounds checking to prevent IndexError
            if project is None or row < 0 or row >= len(self._groups): 
                return
                
            group = self._groups[row]
            project.images[APP_STATE.selected_image].edits.pop(str(group["index"]), None)
            WORKSPACE.save()
            
            try: 
                WORKSPACE.rerender_image(APP_STATE.selected_image)
            except (OSError, ValueError): 
                pass
                
            self._load_image(APP_STATE.selected_image, row)

    def _begin_manual_box(self) -> None:
        project = WORKSPACE.current
        index = APP_STATE.selected_image
        if project is None or not (0 <= index < len(project.images)):
            return
        if self.original.begin_manual_selection():
            self.status.setText("Draw a rectangle around one translatable text area")

    def _manual_rect_created(self, rect: list[int]) -> None:
        if not WORKSPACE.request_manual_region(APP_STATE.selected_image, rect):
            self.status.setText("Manual translation is already running")

    def _on_manual_region_busy(self, busy: bool) -> None:
        self.add_box.setEnabled(not busy)
        if busy:
            self.status.setText("Reading and translating the selected text...")

    def _on_manual_region_finished(self, image_index: int, key: str) -> None:
        if image_index != APP_STATE.selected_image:
            return
        self._load_image(image_index)
        row = next((index for index, group in enumerate(self._groups) if str(group.get("index")) == key), -1)
        if row >= 0:
            APP_STATE.select(image_index, row)
        self.status.setText("Manual text box translated")

    def _on_manual_region_failed(self, image_index: int, message: str) -> None:
        if image_index == APP_STATE.selected_image:
            self.status.setText("Manual translation failed")
        QMessageBox.warning(self, "Manual text box", message)

    def _remove_selected_block(self) -> None:
        row = APP_STATE.selected_block
        if not (0 <= row < len(self._groups)):
            return
        group = self._groups[row]
        try:
            if group.get("manual"):
                removed = WORKSPACE.delete_manual_region(APP_STATE.selected_image, str(group["index"]))
                message = "Manual text box deleted; its automatic blocks were restored"
            else:
                removed = WORKSPACE.suppress_auto_region(APP_STATE.selected_image, int(group["index"]))
                message = "Automatic block removed; draw an Add Text Box replacement if needed"
            if removed:
                self._load_image(APP_STATE.selected_image, max(-1, row - 1))
                self.status.setText(message)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            QMessageBox.warning(self, "Could not remove text block", str(error))

    def _restore_auto_blocks(self) -> None:
        try:
            if WORKSPACE.restore_auto_regions(APP_STATE.selected_image):
                self._load_image(APP_STATE.selected_image)
                self.status.setText("Removed automatic blocks restored")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            QMessageBox.warning(self, "Could not restore automatic blocks", str(error))

    def _choose_color(self) -> None:
        color = QColorDialog.getColor(QColor(self.color.text()), self)
        if color.isValid(): self._update_color_swatch(color.name())

    def _update_color_swatch(self, value: str) -> None:
        color = QColor(value)
        if not color.isValid(): color = QColor("#111111")
        pixmap = QPixmap(18, 18); pixmap.fill(color)
        self.color.setIcon(QIcon(pixmap)); self.color.setText(color.name())
        self.color.setToolTip(f"Choose text color ({color.name()})")

    def _update_font_preview(self, family: str) -> None:
        if family == "Arial Bold":
            self.font.setFont(QFont("Arial", weight=QFont.Weight.Bold))
        else:
            self.font.setFont(QFont(family))

    def _speak_original(self) -> None:
        if not (0 <= APP_STATE.selected_block < len(self._groups)):
            return
        group = self._groups[APP_STATE.selected_block]
        language = group.get("source_language")
        if WORKSPACE.current is not None:
            image_language = WORKSPACE.current.images[APP_STATE.selected_image].source_language
            language = resolve_source_language(WORKSPACE.current.source_language, image_language, language)
        self.speech.speak(str(group.get("original_text", "")), str(language or ""))

    def _open_settings(self) -> None:
        if SettingsDialog(self).exec() == QDialog.DialogCode.Accepted and WORKSPACE.current is not None:
            self.status.setText("Translation provider settings saved")

    def _update_project_title(self) -> None:
        width = max(150, min(420, self.width() // 3))
        self.project_title.setText(QFontMetrics(self.project_title.font()).elidedText(
            self._project_title_full, Qt.TextElideMode.ElideMiddle, width,
        ))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_project_title()

    def _move_image(self, delta: int) -> None:
        if WORKSPACE.current and WORKSPACE.current.images: APP_STATE.select(max(0, min(len(WORKSPACE.current.images)-1, APP_STATE.selected_image + delta)))

    def _fit_both(self) -> None: self.original.fit_image(); self.translated.fit_image()
    def _actual_both(self) -> None: self.original.actual_size(); self.translated.actual_size()

    def _sync_scrollbars(self, source: CanvasView, target: CanvasView) -> None:
        source.horizontalScrollBar().valueChanged.connect(lambda value: target.horizontalScrollBar().setValue(value))
        source.verticalScrollBar().valueChanged.connect(lambda value: target.verticalScrollBar().setValue(value))

    def _toggle_job_panel(self) -> None:
        if not self._has_job_details:
            return
        self._set_job_panel_expanded(not self._job_panel_expanded, user=True)

    def _set_job_panel_expanded(self, expanded: bool, user: bool = False) -> None:
        if user:
            self._job_collapse_timer.stop()
            self._job_manually_collapsed = not expanded
        self._job_panel_expanded = bool(expanded and self._has_job_details)
        self.job_body.setVisible(self._job_panel_expanded)
        self.job_toggle.setArrowType(Qt.ArrowType.DownArrow if self._job_panel_expanded else Qt.ArrowType.RightArrow)
        self.job_toggle.setToolTip("Hide translation job details" if self._job_panel_expanded else "Show translation job details")

    def _begin_job_panel(self) -> None:
        self._job_collapse_timer.stop()
        self._has_job_details = True
        self._terminal_job_state = ""
        self._job_manually_collapsed = False
        self.job_toggle.setEnabled(True)
        self._set_job_panel_expanded(True)

    def _schedule_job_panel_collapse(self, terminal_state: str) -> None:
        self._terminal_job_state = terminal_state
        if self._job_panel_expanded:
            self._job_collapse_timer.start()

    def _auto_collapse_job_panel(self) -> None:
        self._set_job_panel_expanded(False)

    def _on_pipeline(self, stage: str, current: int, total: int, message: str) -> None:
        if not stage:
            self._reset_progress_display()
            return

        if stage == "analyzing" or (stage in self._PROGRESS_RANGES and not self._has_job_details):
            self._begin_job_panel()

        self.status.setText(message)
        self._progress_stage = stage
        if total > 0:
            self._job_total = total

        if stage in self._PROGRESS_RANGES:
            floor, self._page_progress_ceiling = self._PROGRESS_RANGES[stage]
            if stage == "analyzing":
                self._job_position = 1 if self._job_total else 0
                self._completed_pages = 0
                self._page_progress_value = 0.0
                self._job_failure_count = 0
            else:
                self._current_job_filename = self._progress_filename(current, message)
                if current != self._job_position:
                    self._job_position = current
                    self._completed_pages = max(0, current - 1)
                    self._page_progress_value = floor
                else:
                    self._page_progress_value = max(self._page_progress_value, floor)
            self._active_page_in_overall = True
            if not self._progress_timer.isActive():
                self._progress_timer.start()
        elif stage == "complete":
            self._progress_timer.stop()
            self._job_position = current
            self._completed_pages = current
            self._active_page_in_overall = False
            self._page_progress_value = 100.0
            self._current_job_filename = self._progress_filename(current, message) or self._current_job_filename
        elif stage == "failed":
            self._progress_timer.stop()
            self._job_position = max(self._job_position, current)
            self._completed_pages = current
            self._active_page_in_overall = False
            self._job_failure_count += 1
            self._current_job_filename = self._progress_filename(current, message) or self._current_job_filename
        elif stage == "cancelled":
            self._progress_timer.stop()
        elif stage == "ready":
            self._progress_timer.stop()
            self._completed_pages = self._job_total
            self._active_page_in_overall = False
            if self._job_failure_count == 0:
                self._page_progress_value = 100.0
                self.status.setText("Translation complete")
            else:
                self.status.setText(f"Translation finished with {self._job_failure_count} failed page(s)")

        self._update_progress_display()
        display_stage = "failed" if stage == "ready" and self._job_failure_count else stage
        self.stage_status.setText(self._stage_text(display_stage))
        if stage == "ready":
            self._schedule_job_panel_collapse("failed" if self._job_failure_count else "complete")
        elif stage == "cancelled" and not self._job_is_busy:
            self._schedule_job_panel_collapse("cancelled")

    def _advance_progress_animation(self) -> None:
        if self._progress_stage not in self._PROGRESS_RANGES:
            self._progress_timer.stop()
            return
        remaining = self._page_progress_ceiling - self._page_progress_value
        if remaining <= 0.01:
            self._page_progress_value = self._page_progress_ceiling
            self._update_progress_display()
            return
        self._page_progress_value = min(
            self._page_progress_ceiling,
            self._page_progress_value + max(0.1, remaining * 0.04),
        )
        self._update_progress_display()

    def _update_progress_display(self) -> None:
        page_value = max(0.0, min(100.0, self._page_progress_value))
        active_fraction = page_value / 100.0 if self._active_page_in_overall else 0.0
        overall = 0.0
        if self._job_total:
            overall = min(100.0, (self._completed_pages + active_fraction) / self._job_total * 100.0)
        if self._progress_stage == "ready":
            overall = 100.0

        self.progress.setValue(round(overall * 10)); self.progress.setFormat(f"{overall:.1f}%")
        self.page_progress.setValue(round(page_value * 10)); self.page_progress.setFormat(f"{page_value:.1f}%")
        self.progress.setVisible(True); self.page_progress.setVisible(True)

        stage_name = {
            "analyzing": "Preparing", "ocr": "Reading", "translating": "Translating",
            "reconstructing": "Rebuilding", "complete": "Complete", "failed": "Failed",
            "cancelled": "Cancelled", "ready": "Complete" if self._job_failure_count == 0 else "Finished with errors",
        }.get(self._progress_stage, "Waiting")
        if self._job_total:
            position = max(1, min(self._job_position or 1, self._job_total))
            filename = f" • {self._current_job_filename}" if self._current_job_filename else ""
            self.current_page_label.setText(f"Page {position}/{self._job_total} • {stage_name}{filename}")
            if self._progress_stage == "ready" and self._job_failure_count:
                summary = f"Finished with errors • {self._job_failure_count} failed"
            elif self._progress_stage == "ready":
                summary = "100.0% • Complete"
            elif self._progress_stage == "cancelled":
                summary = f"{overall:.1f}% • Cancelled"
            else:
                subject = self._current_job_filename or f"page {position}/{self._job_total}"
                summary = f"{overall:.1f}% • {stage_name} {subject}"
            self.job_overall.setText(summary)
        else:
            self.current_page_label.setText(stage_name)
            self.job_overall.setText("Idle")

    @staticmethod
    def _progress_filename(position: int, message: str) -> str:
        if WORKSPACE.current is not None and WORKSPACE.active_job_ids and position > 0:
            job_ids = WORKSPACE.active_job_ids
            if position <= len(job_ids):
                image_id = job_ids[position - 1]
                image = next((item for item in WORKSPACE.current.images if item.id == image_id), None)
                if image is not None:
                    return Path(image.source_path).name
        for separator in (" text in ", ": ", " "):
            if separator in message:
                candidate = message.rsplit(separator, 1)[-1].strip()
                if "." in candidate:
                    return Path(candidate).name
        return ""

    def _reset_progress_display(self) -> None:
        self._progress_timer.stop()
        self._job_collapse_timer.stop()
        self._page_progress_value = 0.0; self._page_progress_ceiling = 0.0
        self._job_position = 0; self._job_total = 0; self._completed_pages = 0
        self._active_page_in_overall = False; self._progress_stage = ""; self._job_failure_count = 0
        self._current_job_filename = ""
        self.progress.setValue(0); self.progress.setFormat("0.0%"); self.progress.hide()
        self.page_progress.setValue(0); self.page_progress.setFormat("0.0%"); self.page_progress.hide()
        self._has_job_details = False; self._terminal_job_state = ""; self._job_manually_collapsed = False
        self.job_toggle.setEnabled(False); self._set_job_panel_expanded(False)
        self.current_page_label.setText("Current page"); self.job_overall.setText("Idle")
        self.status.setText("Ready"); self.stage_status.setText(self._stage_text(""))

    @staticmethod
    def _stage_text(active: str) -> str:
        stages = [("ocr", "Reading"), ("translating", "Translating"), ("localizing", "Localizing"), ("reconstructing", "Rebuilding"), ("complete", "Complete")]
        active_index = next((index for index, (key, _) in enumerate(stages) if key == active), -1)
        if active in {"ready"}:
            active_index = len(stages)
        values = []
        for index, (_, label) in enumerate(stages):
            marker = "[x]" if index < active_index or active == "ready" else ("[>]" if index == active_index else "[ ]")
            values.append(f"{marker} {label}")
        suffix = "     [!] Failed" if active == "failed" else ("     [!] Cancelled" if active == "cancelled" else "")
        return "     ".join(values) + suffix

    def _on_busy(self, busy: bool) -> None:
        self._job_is_busy = busy
        self.filmstrip.set_reorder_enabled(not busy)
        pending = bool(WORKSPACE.current and any(image.status in {"queued", "partial", "failed", "cancelled"} for image in WORKSPACE.current.images))
        self.start_button.setEnabled(not busy and pending); self.cancel_button.setEnabled(busy); self.close_button.setEnabled(not busy)
        self._selection_changed()
        keep_result = self._job_total > 0 and self._progress_stage in {"ready", "cancelled", "failed", "complete"}
        self.progress.setVisible(busy or keep_result); self.page_progress.setVisible(busy or keep_result)
        if not busy and not keep_result:
            self.progress.setValue(0); self.page_progress.setValue(0)

    def _export(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Export translated images")
        if folder:
            count = WORKSPACE.export(Path(folder)); QMessageBox.information(self, "Export complete", f"Exported {count} image(s).")


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
        self.stack.setCurrentWidget(self.landing)

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
