"""Main translation workspace screen."""


from __future__ import annotations

import json
import re
from pathlib import Path

from PySide6.QtCore import QModelIndex, QRectF, QSize, Qt, QThread, QTimer, Signal, QObject, Slot
from PySide6.QtGui import QAction,QCursor, QColor, QFont, QFontMetrics, QIcon, QIntValidator, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGraphicsView, QGridLayout, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter, QStackedWidget, QTabWidget, QSizePolicy, QTextEdit, QToolButton, QVBoxLayout, QWidget, QKeySequenceEdit, QLineEdit

from hydra_manga_tl.core.assets import find_asset
from hydra_manga_tl.project.editor import RegionEdit
from hydra_manga_tl.project.import_scan import ThumbnailWorker
from hydra_manga_tl.core.language import resolve_source_language
from hydra_manga_tl.project.manual_region import normalize_image_rect, rect_to_polygon
from hydra_manga_tl.core.region_types import normalize_region_type
from hydra_manga_tl.core.settings import SETTINGS
from hydra_manga_tl.core.speech import SpeechService
from hydra_manga_tl.core.state import APP_STATE
from hydra_manga_tl.core.user_errors import export_error, hydra_ai_error, pipeline_error, render_error, workspace_action_error
from hydra_manga_tl.ui.canvas import CanvasView
from hydra_manga_tl.ui.dialogs import AiCenterDialog, BackgroundWorkDialog, ExportOptionsDialog, GlossaryDialog, IdentityPreviewDialog, PhraseMemoryManagerDialog, SettingsDialog, WorkingDialog
from hydra_manga_tl.ui.filmstrip import ReorderableFilmstrip
from hydra_manga_tl.ui.shared import CollapsibleSection, FILMSTRIP_CARD_SIZE, FILMSTRIP_PREVIEW_SIZE, TARGET_LANGUAGE_NAMES, _language_badge, _page_label, _speaker_icon, lucide_icon, confirm
from hydra_manga_tl.project.workspace import WORKSPACE
from hydra_manga_tl.core.ai_bridge import HYDRA_AI


TRANSLATE_ELIGIBLE_STATUSES = {"pending", "queued", "failed", "cancelled"}


class ExportWorker(QObject):
    progress = Signal(int, int)
    finished = Signal(str, object)
    failed = Signal(str)

    def __init__(self, output_type: str, destination: Path, *, image_format: str = "png", archive_format: str = "zip", parent=None) -> None:
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


class WorkspaceScreen(QWidget):
    close_requested = Signal()
    _ART_APPEARANCE_TYPES = {"title", "sfx", "sign", "credit"}
    _HEADER_COMPACT_ENTER_WIDTH = 1340
    _HEADER_COMPACT_EXIT_WIDTH = 1440

    _PROGRESS_RANGES = {
        "preprocessing": (0.0, 5.0),
        "analyzing": (0.0, 5.0),
        "OCR": (5.0, 30.0),
        "ocr": (5.0, 30.0),
        "translating": (30.0, 50.0),
        "rendering": (50.0, 90.0),
        "reconstructing": (50.0, 98.0),
        "review": (90.0, 98.0),
    }

    def _register_responsive_action(self, button: QWidget) -> None:
        text = button.text() if hasattr(button, "text") else ""
        if text:
            button.setProperty("fullText", text)
            auto_tooltip = not button.toolTip()
            button.setProperty("responsiveAutoTooltip", auto_tooltip)
            if auto_tooltip:
                button.setToolTip(text)
            if not button.accessibleName():
                button.setAccessibleName(text)
        if hasattr(button, "setIconSize"):
            button.setIconSize(QSize(16, 16))
        button.setMinimumWidth(0)
        policy = button.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
        button.setSizePolicy(policy)
        self._responsive_action_buttons.append(button)
        if text:
            self._set_responsive_button_text(button, text)

    def _set_responsive_button_text(self, button: QWidget, text: str) -> None:
        button.setProperty("fullText", text)
        if text and button.property("responsiveAutoTooltip"):
            button.setToolTip(text)
        elif text and not button.toolTip():
            button.setToolTip(text)
        if text and not button.accessibleName():
            button.setAccessibleName(text)
        if isinstance(button, QToolButton):
            button.setText(text)
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonIconOnly
                if self._header_compact
                else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
        elif isinstance(button, QPushButton):
            button.setText("" if self._header_compact else text)

    def _set_header_compact(self, compact: bool) -> None:
        if compact == self._header_compact:
            return
        self._header_compact = compact
        for label in self._responsive_field_labels:
            label.setVisible(not compact)
            policy = label.sizePolicy()
            policy.setHorizontalPolicy(
                QSizePolicy.Policy.Ignored if compact else QSizePolicy.Policy.Preferred
            )
            label.setSizePolicy(policy)
        for button in self._responsive_action_buttons:
            wide_width = int(button.property("responsiveWideWidth") or 156)
            wide_min_width = int(button.property("responsiveMinWidth") or 36)
            scope = str(button.property("responsiveScope") or "header")
            if scope == "toolstrip":
                compact_width = int(button.property("responsiveCompactWidth") or 38)
                button.setMaximumWidth(compact_width if compact else wide_width)
                button.setMinimumWidth(38 if compact else wide_min_width)
            else:
                button.setMaximumWidth(46 if compact else wide_width)
                button.setMinimumWidth(36 if compact else wide_min_width)
            policy = button.sizePolicy()
            if scope == "toolstrip" and compact:
                policy.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
            else:
                policy.setHorizontalPolicy(
                    QSizePolicy.Policy.Ignored if compact else QSizePolicy.Policy.Expanding
                )
            button.setSizePolicy(policy)
            text = str(button.property("fullText") or "")
            if text:
                self._set_responsive_button_text(button, text)

    def _update_header_responsive_mode(self) -> None:
        width = self.width()
        near_effective_minimum = width <= self.minimumSizeHint().width() + 24
        if width >= self._HEADER_COMPACT_EXIT_WIDTH:
            self._set_header_compact(False)
        elif width < self._HEADER_COMPACT_ENTER_WIDTH or near_effective_minimum:
            self._set_header_compact(True)

    @staticmethod
    def _compact_project_title(title: str, *, max_parts: int = 2, max_chars: int = 24) -> str:
        cleaned = title.strip()
        if not cleaned:
            return "Project"
        parts = [
            part
            for part in re.split(r"[\s_\-–—|:;,.()\[\]{}]+", cleaned)
            if part
        ]
        if not parts:
            return cleaned[:max_chars].rstrip()
        compact = " ".join(parts[:max_parts])
        if len(compact) > max_chars:
            compact = compact[:max_chars].rstrip()
        return compact or "Project"

    def __init__(self) -> None:
        super().__init__()
        self._syncing = False
        self._groups: list[dict] = []
        self._project_title_full = "Project"
        self._filmstrip_project_id = ""
        self._filmstrip_policy_project_id = ""
        self._filmstrip_policy_mode = SETTINGS.filmstrip_collapse_mode or "current"
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
        self._manual_busy = False
        self._manual_shortcut: QShortcut | None = None
        self._title_reconstruction_shortcut: QShortcut | None = None
        self._region_cycle_mode = SETTINGS.manual_region_mode or "rectangle"
        self._manual_creation_kind = "region"
        self._editor_shortcuts: list[tuple[QShortcut, bool]] = []
        self._layout_undo: list[dict] = []
        self._layout_redo: list[dict] = []
        self._filmstrip_undo: list[dict] = []
        self._filmstrip_redo: list[dict] = []
        self._pending_text_layouts: dict[tuple[int, str], dict] = {}
        self._pending_manual_history: dict[int, dict] = {}
        self._ignore_next_open_page_selection: int = 0  # countdown: absorbs N selection_changed signals after project open
        self._recent_manual_requests: dict[str, object] = {}
        self._responsive_action_buttons: list[QWidget] = []
        self._responsive_field_labels: list[QLabel] = []
        self._header_compact = False
        self.speech = SpeechService(self)
        self.speech.unavailable.connect(lambda message: QMessageBox.information(self, "Original text voice", message))
        self._build()
        # Start with compact metrics so Qt can still reach the narrow workspace width.
        self._set_header_compact(True)
        self._configure_manual_shortcut()
        self._progress_timer = QTimer(self); self._progress_timer.setInterval(100)
        self._progress_timer.timeout.connect(self._advance_progress_animation)
        self._job_collapse_timer = QTimer(self); self._job_collapse_timer.setSingleShot(True); self._job_collapse_timer.setInterval(3000)
        self._job_collapse_timer.timeout.connect(self._auto_collapse_job_panel)
        APP_STATE.project_changed.connect(self.refresh)
        APP_STATE.selection_changed.connect(self._on_selection)
        APP_STATE.pipeline_changed.connect(self._on_pipeline)
        APP_STATE.busy_changed.connect(self._on_busy)
        WORKSPACE.image_updated.connect(self._on_workspace_image_updated)
        WORKSPACE.manual_region_finished.connect(self._on_manual_region_finished)
        WORKSPACE.manual_region_failed.connect(self._on_manual_region_failed)
        WORKSPACE.manual_region_busy_changed.connect(self._on_manual_region_busy)
        WORKSPACE.translation_request_state_changed.connect(
            self._on_translation_request_state,
        )

    def _build(self) -> None:
        root = QVBoxLayout(self); root.setContentsMargins(12, 10, 12, 8); root.setSpacing(8)
        header = QFrame(); header.setObjectName("Header")
        row = QHBoxLayout(header); row.setContentsMargins(10, 8, 10, 8); row.setSpacing(8)
        header_icon = lucide_icon("book-open")

        def header_group(*widgets: QWidget) -> QFrame:
            group = QFrame()
            group.setObjectName("HeaderGroup")
            layout = QHBoxLayout(group)
            layout.setContentsMargins(7, 4, 7, 4)
            layout.setSpacing(6)
            for item in widgets:
                layout.addWidget(item)
            return group

        def field_label(text: str) -> QLabel:
            label = QLabel(text)
            label.setObjectName("ToolbarLabel")
            label.setMinimumWidth(0)
            policy = label.sizePolicy()
            policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
            label.setSizePolicy(policy)
            self._responsive_field_labels.append(label)
            return label

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
        self.cancel_button = QPushButton("Cancel"); self.cancel_button.clicked.connect(WORKSPACE.cancel_active_requests); self.cancel_button.setEnabled(False)
        save = QPushButton("Save"); save.clicked.connect(WORKSPACE.save)
        export = QPushButton("Export"); export.clicked.connect(self._export)
        self.close_button = QPushButton("Close"); self.close_button.clicked.connect(self.close_requested)
        settings = QPushButton("Settings"); settings.clicked.connect(self._open_settings)
        ai_center = QPushButton("AI Center"); ai_center.clicked.connect(lambda: AiCenterDialog(self).exec())
        glossary = QPushButton("Glossary"); glossary.clicked.connect(lambda: GlossaryDialog(self).exec())
        self.project_title.setPixmap(header_icon.pixmap(QSize(18, 18)))
        self.project_title.setText("  Project")
        self.selected_button.setIcon(lucide_icon("send"))
        self.start_button.setIcon(lucide_icon("play"))
        self.cancel_button.setIcon(lucide_icon("square-x"))
        save.setIcon(lucide_icon("save"))
        export.setIcon(lucide_icon("download"))
        settings.setIcon(lucide_icon("settings"))
        ai_center.setIcon(lucide_icon("message-circle-warning"))
        glossary.setIcon(lucide_icon("book-open"))
        self.close_button.setIcon(lucide_icon("x"))
        for button in (
            self.selected_button,
            self.start_button,
            self.cancel_button,
            glossary,
            ai_center,
            settings,
            save,
            export,
            self.close_button,
        ):
            self._register_responsive_action(button)
        self.selected_button.setProperty("responsiveWideWidth", 215)
        self.selected_button.setProperty("responsiveMinWidth", 170)
        self.start_button.setProperty("responsiveWideWidth", 210)
        self.start_button.setProperty("responsiveMinWidth", 180)
        self.cancel_button.setProperty("responsiveWideWidth", 120)
        self.cancel_button.setProperty("responsiveMinWidth", 92)
        translate_group = header_group(self.selected_button, self.start_button, self.cancel_button)
        translate_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(header_group(self.project_title, self.count_label))
        row.addWidget(header_group(field_label("Source"), self.source_combo, field_label("Target"), self.target_combo, field_label("Quality"), self.quality_combo, field_label("Style"), self.style_combo))
        row.addWidget(translate_group, 1)
        row.addWidget(header_group(glossary, ai_center, settings))
        row.addWidget(header_group(save, export, self.close_button))
        root.addWidget(header)

        tools = QHBoxLayout()
        previous = QPushButton("‹"); previous.clicked.connect(lambda: self._move_image(-1))
        next_button = QPushButton("›"); next_button.clicked.connect(lambda: self._move_image(1))
        fit = QPushButton("Fit"); fit.clicked.connect(self._fit_both)
        actual = QPushButton("100%"); actual.clicked.connect(self._actual_both)
        self.next_ocr_issue = QPushButton("Next OCR"); self.next_ocr_issue.clicked.connect(self._next_ocr_issue)
        self.next_review_issue = QPushButton("Next Review"); self.next_review_issue.clicked.connect(self._next_review_issue)
        self.next_ocr_issue.setToolTip("Next OCR Issue")
        self.next_review_issue.setToolTip("Next Review Issue")
        previous.setObjectName("ToolIconButton"); next_button.setObjectName("ToolIconButton")
        fit.setIcon(lucide_icon("maximize"))
        actual.setIcon(lucide_icon("scan-text"))
        self.next_ocr_issue.setIcon(lucide_icon("scan-text"))
        self.next_review_issue.setIcon(lucide_icon("message-circle-warning"))
        self.add_box = QToolButton()
        self.add_box.setObjectName("ToolbarButton")
        self.add_box.setText("Region Tool")
        self.add_box.setIcon(lucide_icon("box"))
        self.add_box.setCheckable(True)
        self.add_box.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.add_box.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        region_menu = QMenu(self.add_box)
        self.rectangle_region_action = QAction("Rectangle", self)
        self.rectangle_region_action.triggered.connect(lambda: self._begin_manual_box("rectangle", kind="region"))
        self.polygon_region_action = QAction("Polygon", self)
        self.polygon_region_action.triggered.connect(lambda: self._begin_manual_box("polygon", kind="region"))
        region_menu.addAction(self.rectangle_region_action)
        region_menu.addAction(self.polygon_region_action)
        self.add_box.setMenu(region_menu)
        self.add_box.clicked.connect(lambda: self._begin_manual_box(SETTINGS.manual_region_mode or "rectangle", kind="region"))
        self.title_reconstruction = QToolButton()
        self.title_reconstruction.setObjectName("ToolbarButton")
        self.title_reconstruction.setText("Title Recon")
        self.title_reconstruction.setIcon(lucide_icon("type"))
        self.title_reconstruction.setToolTip("Title Reconstruction")
        self.title_reconstruction.setCheckable(True)
        self.title_reconstruction.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.title_reconstruction.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        title_menu = QMenu(self.title_reconstruction)
        self.rectangle_title_action = QAction("Rectangle", self)
        self.rectangle_title_action.triggered.connect(lambda: self._begin_manual_box("rectangle", kind="title"))
        self.polygon_title_action = QAction("Polygon", self)
        self.polygon_title_action.triggered.connect(lambda: self._begin_manual_box("polygon", kind="title"))
        title_menu.addAction(self.rectangle_title_action)
        title_menu.addAction(self.polygon_title_action)
        self.title_reconstruction.setMenu(title_menu)
        self.title_reconstruction.clicked.connect(lambda: self._begin_manual_box(SETTINGS.manual_region_mode or "rectangle", kind="title"))
        self.bubble_selector = QToolButton()
        self.bubble_selector.setObjectName("ToolbarButton")
        self.bubble_selector.setText("Bubble Selector")
        self.bubble_selector.setIcon(lucide_icon("message-square"))
        self.bubble_selector.setCheckable(True)
        self.bubble_selector.setToolTip("Click or drag marquee box to select multiple text bubbles on original page")
        self.bubble_selector.clicked.connect(self._toggle_bubble_selector)
        self.image_label = QLabel("No image")
        self.selection_label = QLabel("1 selected"); self.selection_label.setObjectName("Muted")
        self.select_pending_button = QPushButton("Select Pending")
        self.select_pending_button.setObjectName("SecondaryButton")
        self.select_pending_button.setIcon(lucide_icon("scan-text"))
        self.select_pending_button.setToolTip("Select every page in the filmstrip that can be translated")
        self.select_pending_button.clicked.connect(self._select_pending_images)
        self.clear_selection_button = QPushButton("Clear")
        self.clear_selection_button.setObjectName("SecondaryButton")
        self.clear_selection_button.setIcon(lucide_icon("x"))
        self.clear_selection_button.setToolTip("Clear filmstrip page selection batch and canvas text bubble selections")
        self.clear_selection_button.clicked.connect(self._clear_all_selections)
        for button in (
            self.select_pending_button,
            self.clear_selection_button,
            self.next_ocr_issue,
            self.next_review_issue,
            self.bubble_selector,
            self.add_box,
            self.title_reconstruction,
            fit,
            actual,
        ):
            self._register_responsive_action(button)
            button.setProperty("responsiveScope", "toolstrip")
            button.setProperty("responsiveCompactWidth", 84)
        self.select_pending_button.setProperty("responsiveWideWidth", 180)
        self.select_pending_button.setProperty("responsiveMinWidth", 150)
        self.clear_selection_button.setProperty("responsiveWideWidth", 125)
        self.clear_selection_button.setProperty("responsiveMinWidth", 82)
        self.next_ocr_issue.setProperty("responsiveWideWidth", 155)
        self.next_ocr_issue.setProperty("responsiveMinWidth", 135)
        self.next_review_issue.setProperty("responsiveWideWidth", 170)
        self.next_review_issue.setProperty("responsiveMinWidth", 150)
        self.bubble_selector.setProperty("responsiveWideWidth", 185)
        self.bubble_selector.setProperty("responsiveMinWidth", 155)
        self.add_box.setProperty("responsiveWideWidth", 160)
        self.add_box.setProperty("responsiveMinWidth", 125)
        self.title_reconstruction.setProperty("responsiveWideWidth", 185)
        self.title_reconstruction.setProperty("responsiveMinWidth", 155)
        fit.setProperty("responsiveWideWidth", 105)
        fit.setProperty("responsiveMinWidth", 80)
        actual.setProperty("responsiveWideWidth", 110)
        actual.setProperty("responsiveMinWidth", 82)
        tool_frame = QFrame(); tool_frame.setObjectName("ToolStrip")
        tool_frame.setLayout(tools)
        tool_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tools.setContentsMargins(0, 0, 0, 0); tools.setSpacing(8)

        nav_group = QFrame(); nav_group.setObjectName("ToolStripGroup")
        nav_layout = QHBoxLayout(nav_group); nav_layout.setContentsMargins(8, 6, 8, 6); nav_layout.setSpacing(7)
        nav_layout.addWidget(previous); nav_layout.addWidget(next_button)
        nav_layout.addWidget(self.image_label); nav_layout.addWidget(self.selection_label)
        nav_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        action_group = QFrame(); action_group.setObjectName("ToolStripGroup")
        action_layout = QHBoxLayout(action_group); action_layout.setContentsMargins(8, 6, 8, 6); action_layout.setSpacing(7)
        action_layout.addWidget(self.select_pending_button, 1)
        action_layout.addWidget(self.clear_selection_button, 1)
        action_layout.addWidget(self.next_ocr_issue, 1)
        action_layout.addWidget(self.next_review_issue, 1)
        action_layout.addWidget(self.bubble_selector, 1)
        action_layout.addWidget(self.add_box, 1)
        action_layout.addWidget(self.title_reconstruction, 1)
        action_layout.addWidget(fit, 1)
        action_layout.addWidget(actual, 1)
        action_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        tools.addWidget(nav_group)
        tools.addWidget(action_group, 1)
        tool_scroll = QScrollArea()
        tool_scroll.setObjectName("ToolStripScroll")
        tool_scroll.setWidget(tool_frame)
        tool_scroll.setWidgetResizable(True)
        tool_scroll.setFrameShape(QFrame.Shape.NoFrame)
        tool_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        tool_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tool_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tool_scroll.setFixedHeight(
            tool_frame.sizeHint().height()
            + tool_scroll.horizontalScrollBar().sizeHint().height()
            + 2
        )
        root.addWidget(tool_scroll)

        main = QSplitter(Qt.Orientation.Horizontal); self.main_splitter = main
        main.setChildrenCollapsible(False)
        canvas_host = QWidget(); canvas_layout = QVBoxLayout(canvas_host); canvas_layout.setContentsMargins(0, 0, 0, 0)
        self.canvas_stack = QStackedWidget()
        canvases = QSplitter(Qt.Orientation.Horizontal)
        self.original = CanvasView("Original"); self.translated = CanvasView("Translated")
        self.original_status = QLabel("Ready"); self.original_status.setObjectName("StatusPill")
        self.translated_status = QLabel("Ready"); self.translated_status.setObjectName("StatusPill")
        canvases.addWidget(self._canvas_panel("Original", self.original_status, self.original))
        canvases.addWidget(self._canvas_panel("Translated", self.translated_status, self.translated))
        canvases.setSizes([600, 600])
        self.page_canvases = canvases
        self.identity_preview = CanvasView("Hydra Identity")
        self.identity_preview.setObjectName("IdentityWorkspacePreview")
        self.identity_status = QLabel("Preview"); self.identity_status.setObjectName("StatusPill")
        self.canvas_stack.addWidget(self.page_canvases)
        self.canvas_stack.addWidget(self.identity_preview)
        canvas_layout.addWidget(self.canvas_stack)
        self.filmstrip_section = CollapsibleSection("Filmstrip", expanded=True)
        self.filmstrip_section.setObjectName("FilmstripSection")
        self.filmstrip_section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.filmstrip_section.body.setMaximumHeight(132)
        self.filmstrip_section.expanded_changed.connect(self._filmstrip_expanded_changed)
        jump_host = QWidget()
        jump_layout = QHBoxLayout(jump_host)
        jump_layout.setContentsMargins(0, 0, 0, 0)
        jump_layout.setSpacing(5)
        jump_label = QLabel("Jump")
        jump_label.setObjectName("Muted")
        self.filmstrip_jump = QLineEdit()
        self.filmstrip_jump.setValidator(QIntValidator(1, 999999, self.filmstrip_jump))
        self.filmstrip_jump.setPlaceholderText("Page")
        self.filmstrip_jump.setToolTip("Jump to page number")
        self.filmstrip_jump.setFixedWidth(54)
        self.filmstrip_jump.setEnabled(False)
        self.filmstrip_jump.returnPressed.connect(self._jump_to_filmstrip_page)
        self.filmstrip_jump_button = QPushButton("Go")
        self.filmstrip_jump_button.setFixedWidth(54)
        self.filmstrip_jump_button.setToolTip("Jump to the entered page number")
        self.filmstrip_jump_button.setEnabled(False)
        self.filmstrip_jump_button.clicked.connect(self._jump_to_filmstrip_page)
        jump_layout.addWidget(jump_label)
        jump_layout.addWidget(self.filmstrip_jump)
        jump_layout.addWidget(self.filmstrip_jump_button)
        self.filmstrip_jump_host = jump_host
        filmstrip_header = QWidget()
        filmstrip_header_layout = QHBoxLayout(filmstrip_header)
        filmstrip_header_layout.setContentsMargins(0, 0, 0, 0)
        filmstrip_header_layout.setSpacing(6)
        filmstrip_section_layout = self.filmstrip_section.layout()
        filmstrip_section_layout.removeWidget(self.filmstrip_section.toggle)
        self.filmstrip_section.toggle.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        filmstrip_header_layout.addWidget(self.filmstrip_section.toggle)
        filmstrip_header_layout.addWidget(jump_host)
        filmstrip_header_layout.addStretch(1)
        self.filmstrip_header = filmstrip_header
        filmstrip_section_layout.insertWidget(0, filmstrip_header)
        filmstrip_layout = QHBoxLayout(self.filmstrip_section.body)
        filmstrip_layout.setContentsMargins(5, 3, 5, 5)
        filmstrip_layout.setSpacing(6)
        self.identity_thumbnail_path = find_asset("thumbnail", "hydra.png")
        self.identity_tile = QToolButton()
        self.identity_tile.setObjectName("IdentityTile")
        self.identity_tile.setText("Hydra")
        self.identity_tile.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.identity_tile.setIconSize(FILMSTRIP_PREVIEW_SIZE)
        self.identity_tile.setFixedSize(FILMSTRIP_CARD_SIZE)
        self.identity_tile.setCheckable(True)
        self.identity_tile.setToolTip("Show the Hydra Manga TL identity preview in the workspace")
        if self.identity_thumbnail_path is not None:
            self.identity_tile.setIcon(QIcon(str(self.identity_thumbnail_path)))
            self.identity_tile.clicked.connect(self._select_identity)
            self.identity_tile.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.identity_tile.customContextMenuRequested.connect(self._identity_tile_menu)
        else:
            self.identity_tile.setEnabled(False)
            self.identity_tile.setToolTip("Hydra identity artwork is unavailable")
        filmstrip_layout.addWidget(self.identity_tile, 0, Qt.AlignmentFlag.AlignTop)
        self.filmstrip = ReorderableFilmstrip(); self.filmstrip.setObjectName("Filmstrip")
        self.filmstrip.setViewMode(QListView.ViewMode.IconMode); self.filmstrip.setFlow(QListView.Flow.LeftToRight)
        self.filmstrip.setResizeMode(QListView.ResizeMode.Adjust); self.filmstrip.setMovement(QListView.Movement.Snap)
        self.filmstrip.setWrapping(False); self.filmstrip.setUniformItemSizes(True); self.filmstrip.setSpacing(5)
        self.filmstrip.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.filmstrip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.filmstrip.setIconSize(FILMSTRIP_PREVIEW_SIZE); self.filmstrip.setMaximumHeight(124); self.filmstrip.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.filmstrip.set_reorder_enabled(True); self.filmstrip.order_changed.connect(self._on_filmstrip_reordered)
        self.filmstrip.add_pages_requested.connect(self._on_add_pages_clicked)
        self.filmstrip.reorder_hint.connect(lambda text: self.status.setText(text) if hasattr(self, "status") else None)
        self.filmstrip.currentRowChanged.connect(self._filmstrip_current_changed)
        self.filmstrip.itemSelectionChanged.connect(self._selection_changed)
        self.filmstrip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.filmstrip.customContextMenuRequested.connect(self._filmstrip_menu)
        filmstrip_layout.addWidget(self.filmstrip, 1, Qt.AlignmentFlag.AlignTop)
        canvas_layout.addWidget(self.filmstrip_section)
        main.addWidget(canvas_host)
        self.inspector = self._build_inspector(); main.addWidget(self.inspector)
        main.setStretchFactor(0, 1); main.setStretchFactor(1, 0)
        self.inspector.setMinimumWidth(440); self.inspector.setMaximumWidth(760); main.setSizes([1200, 640])
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
        self.original.batch_regions_selected.connect(self._batch_blocks_selected)
        self.translated.text_layout_changed.connect(self._text_layout_changed)
        self.original.manual_region_created.connect(self._manual_region_created)
        self.original.manual_region_message.connect(self.status.setText)
        self.original.manual_selection_finished.connect(self._reset_region_tool)
        self.original.zoom_changed.connect(self.translated.set_zoom); self.translated.zoom_changed.connect(self.original.set_zoom)
        self._sync_scrollbars(self.original, self.translated); self._sync_scrollbars(self.translated, self.original)
        self._layout_undo_shortcut = QShortcut(QKeySequence.StandardKey.Undo, self)
        self._layout_undo_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._layout_undo_shortcut.activated.connect(self._undo_workspace)
        self._layout_redo_shortcut = QShortcut(QKeySequence.StandardKey.Redo, self)
        self._layout_redo_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._layout_redo_shortcut.activated.connect(self._redo_workspace)
        self._configure_editor_shortcuts()
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._application_focus_changed)

    def _canvas_panel(self, title: str, status_label: QLabel, canvas: CanvasView) -> QFrame:
        panel = QFrame()
        panel.setObjectName("CanvasPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QFrame()
        header.setObjectName("CanvasPanelHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 7, 10, 7)
        header_layout.setSpacing(7)
        title_label = QLabel(title)
        title_label.setObjectName("CanvasPanelTitle")
        header_layout.addWidget(title_label)
        header_layout.addWidget(status_label)
        header_layout.addStretch(1)
        layout.addWidget(header)
        layout.addWidget(canvas, 1)
        return panel

    @staticmethod
    def _status_label_text(status: str) -> str:
        value = (status or "ready").strip()
        if not value:
            return "Ready"
        return value.replace("_", " ").title()

    def _update_canvas_status(self, status: str) -> None:
        text = self._status_label_text(status)
        state = (status or "ready").strip().casefold() or "ready"
        for label in (self.original_status, self.translated_status):
            label.setText(text)
            label.setProperty("statusState", state)
            label.style().unpolish(label)
            label.style().polish(label)
        if hasattr(self, "identity_status"):
            self.identity_status.setText(text)
            self.identity_status.setProperty("statusState", state)
            self.identity_status.style().unpolish(self.identity_status)
            self.identity_status.style().polish(self.identity_status)

    def _application_focus_changed(self, _old, _new) -> None:
        self._update_editor_shortcuts()

    def _manual_shortcut_sequence(self) -> QKeySequence:
        sequence = QKeySequence(SETTINGS.manual_textbox_shortcut or "Ctrl+D")
        return sequence if not sequence.isEmpty() else QKeySequence("Ctrl+D")

    def _title_reconstruction_shortcut_sequence(self) -> QKeySequence:
        sequence = QKeySequence(SETTINGS.title_reconstruction_shortcut or "Ctrl+F")
        return sequence if not sequence.isEmpty() else QKeySequence("Ctrl+F")

    def _configure_manual_shortcut(self) -> None:
        manual_sequence = self._manual_shortcut_sequence()
        if self._manual_shortcut is None:
            self._manual_shortcut = QShortcut(manual_sequence, self)
            self._manual_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self._manual_shortcut.activated.connect(self._cycle_manual_box_mode)
        else:
            self._manual_shortcut.setKey(manual_sequence)
        manual_label = manual_sequence.toString(QKeySequence.SequenceFormat.NativeText) or "Ctrl+D"
        self.add_box.setToolTip(f"Cycle Region Tool modes ({manual_label})")
        if hasattr(self, "title_reconstruction"):
            title_sequence = self._title_reconstruction_shortcut_sequence()
            if self._title_reconstruction_shortcut is None:
                self._title_reconstruction_shortcut = QShortcut(title_sequence, self)
                self._title_reconstruction_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                self._title_reconstruction_shortcut.activated.connect(self._cycle_title_reconstruction_mode)
            else:
                self._title_reconstruction_shortcut.setKey(title_sequence)
            title_label = title_sequence.toString(QKeySequence.SequenceFormat.NativeText) or "Ctrl+F"
            self.title_reconstruction.setToolTip(f"Cycle Title Reconstruction modes ({title_label})")

    def _register_editor_shortcut(self, sequence: str, callback, *, allow_text_focus: bool = False) -> QShortcut:
        shortcut = QShortcut(QKeySequence(sequence), self)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(callback)
        self._editor_shortcuts.append((shortcut, allow_text_focus))
        return shortcut

    def _configure_editor_shortcuts(self) -> None:
        self._register_editor_shortcut("R", lambda: self._begin_manual_box("rectangle"))
        self._register_editor_shortcut("P", lambda: self._begin_manual_box("polygon"))
        self._register_editor_shortcut("Ctrl+B", self._toggle_bubble_selector_shortcut)
        self._register_editor_shortcut("Ctrl+Return", self._apply, allow_text_focus=True)
        self._register_editor_shortcut("Ctrl+Backspace", self._reset_edit)
        self._register_editor_shortcut("Alt+O", self._next_ocr_issue, allow_text_focus=True)
        self._register_editor_shortcut("Alt+R", self._next_review_issue, allow_text_focus=True)
        self._register_editor_shortcut("F", self._fit_both)
        self._register_editor_shortcut("1", self._actual_both)
        self._register_editor_shortcut("Tab", lambda: self._select_relative_block(1))
        self._register_editor_shortcut("Backtab", lambda: self._select_relative_block(-1))
        self._register_editor_shortcut("Delete", self._delete_selected_context)
        self._register_editor_shortcut("Esc", self._cancel_manual_draw, allow_text_focus=True)
        self._update_editor_shortcuts()

    @staticmethod
    def _shortcut_text_focus() -> bool:
        focus = QApplication.focusWidget()
        return isinstance(focus, (QTextEdit, QLineEdit, QKeySequenceEdit, QComboBox))

    def _update_editor_shortcuts(self) -> None:
        pause_text_shortcuts = self._shortcut_text_focus()
        for shortcut, allow_text_focus in self._editor_shortcuts:
            shortcut.setEnabled(allow_text_focus or not pause_text_shortcuts)

    @staticmethod
    def _next_region_mode(mode: str) -> str:
        return "rectangle" if mode == "polygon" else "polygon"

    def _cycle_manual_box_mode(self) -> None:
        mode = "polygon" if self._region_cycle_mode == "polygon" else "rectangle"
        self._region_cycle_mode = self._next_region_mode(mode)
        self._begin_manual_box(mode)

    def _cycle_title_reconstruction_mode(self) -> None:
        mode = "polygon" if self._region_cycle_mode == "polygon" else "rectangle"
        self._region_cycle_mode = self._next_region_mode(mode)
        self._begin_manual_box(mode, kind="title")

    def _toggle_bubble_selector_shortcut(self) -> None:
        self.bubble_selector.setChecked(not self.bubble_selector.isChecked())
        self._toggle_bubble_selector()

    def _refresh_region_tool_style(self) -> None:
        for button in (self.add_box, getattr(self, "title_reconstruction", None)):
            if button is None:
                continue
            button.style().unpolish(button)
            button.style().polish(button)

    def _set_region_tool_active(self, mode: str, kind: str = "region") -> None:
        label = "Polygon" if mode == "polygon" else "Rectangle"
        if kind == "title":
            self._set_responsive_button_text(self.title_reconstruction, f"Title {label}")
            self.title_reconstruction.setChecked(True)
            self._set_responsive_button_text(self.add_box, "Region Tool")
            self.add_box.setChecked(False)
        else:
            self._set_responsive_button_text(self.add_box, label)
            self.add_box.setChecked(True)
            self._set_responsive_button_text(self.title_reconstruction, "Title Recon")
            self.title_reconstruction.setChecked(False)
        self._refresh_region_tool_style()

    def _reset_region_tool(self) -> None:
        self._set_responsive_button_text(self.add_box, "Region Tool")
        self.add_box.setChecked(False)
        self._set_responsive_button_text(self.title_reconstruction, "Title Recon")
        self.title_reconstruction.setChecked(False)
        if hasattr(self, "bubble_selector"):
            self.bubble_selector.setChecked(False)
        self._refresh_region_tool_style()

    def _build_inspector(self) -> QFrame:
        frame = QFrame(); frame.setObjectName("Inspector"); layout = QVBoxLayout(frame)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.setContentsMargins(10, 8, 10, 8); layout.setSpacing(6)
        tabs = QTabWidget(); layout.addWidget(tabs)
        text_tab = QWidget(); text_layout = QVBoxLayout(text_tab)
        text_layout.setContentsMargins(8, 7, 8, 8); text_layout.setSpacing(7)
        self.blocks = QListWidget(); self.blocks.setObjectName("TextBlocksList"); self.blocks.setWordWrap(True); self.blocks.setTextElideMode(Qt.TextElideMode.ElideRight); self.blocks.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); self.blocks.setMinimumHeight(140); self.blocks.setMaximumHeight(175); self.blocks.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); self.blocks.currentRowChanged.connect(self._select_block); self.blocks.itemSelectionChanged.connect(self._text_blocks_selection_changed)
        text_layout.addWidget(self.blocks)
        editor_host = QWidget(); editor_layout = QVBoxLayout(editor_host)
        editor_host.setMinimumWidth(0); editor_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        editor_layout.setContentsMargins(0, 0, 0, 0); editor_layout.setSpacing(7)
        form_host = QWidget(); form = QFormLayout(form_host); form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form_host.setMinimumWidth(0); form_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setContentsMargins(0, 0, 0, 0); form.setHorizontalSpacing(8); form.setVerticalSpacing(7)
        translation_field_min_width = 120
        self.original_text = QTextEdit(); self.original_text.setReadOnly(False); self.original_text.setFixedHeight(50)
        self.original_text.setToolTip("Correct OCR source text here; approval is separate from Apply & Rerender")
        self.original_text.setMinimumWidth(translation_field_min_width)
        self.original_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        original_host = QWidget(); original_host.setMinimumWidth(0)
        original_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        original_row = QHBoxLayout(original_host); original_row.setContentsMargins(0, 0, 0, 0); original_row.setSpacing(6)
        self.speak_original = QToolButton(); self.speak_original.setObjectName("SpeechButton"); self.speak_original.setIcon(_speaker_icon())
        self.speak_original.setIconSize(QSize(18, 18))
        self.speak_original.setToolTip("Play or stop the original text")
        self.speak_original.setFixedSize(30, 30)
        self.speak_original.setEnabled(False); self.speak_original.clicked.connect(self._speak_original)
        original_row.addWidget(self.original_text, 1); original_row.addWidget(self.speak_original)
        self.translation = QTextEdit(); self.translation.setFixedHeight(50)
        self.translation.setMinimumWidth(translation_field_min_width)
        self.translation.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.confidence = QLabel("—"); self.confidence.setObjectName("Muted")
        self.confidence.setMinimumWidth(0)
        self.confidence.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.confidence_bar = QProgressBar(); self.confidence_bar.setRange(0, 1000); self.confidence_bar.setTextVisible(False); self.confidence_bar.setFixedHeight(8)
        confidence_host = QWidget(); confidence_row = QHBoxLayout(confidence_host); confidence_row.setContentsMargins(0, 0, 0, 0); confidence_row.setSpacing(8)
        confidence_host.setMinimumWidth(0); confidence_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        confidence_row.addWidget(self.confidence, 1); confidence_row.addWidget(self.confidence_bar, 1)
        self.replace = QCheckBox("Replace source text"); self.replace.setChecked(True)
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
        self.bubble_type = QComboBox()
        self._sync_bubble_type_options("dialogue")
        self.bubble_type.currentIndexChanged.connect(self._refresh_appearance_for_selected_type)
        self.color = QPushButton("#111111"); self.color.clicked.connect(self._choose_color)
        self.gradient_enabled = QCheckBox("Use gradient fill"); self.gradient_enabled.toggled.connect(self._update_gradient_controls_enabled)
        self.gradient_start = QPushButton("#111111"); self.gradient_start.clicked.connect(self._choose_gradient_start)
        self.gradient_end = QPushButton("#ffffff"); self.gradient_end.clicked.connect(self._choose_gradient_end)
        self.gradient_angle = QSpinBox(); self.gradient_angle.setRange(0, 180); self.gradient_angle.setSuffix(" deg"); self.gradient_angle.setSingleStep(15)
        self.offset_x = QSpinBox(); self.offset_x.setRange(-500, 500); self.offset_x.setSuffix(" px")
        self.offset_y = QSpinBox(); self.offset_y.setRange(-500, 500); self.offset_y.setSuffix(" px")
        self.offset_angle = QDoubleSpinBox(); self.offset_angle.setRange(-180.0, 180.0); self.offset_angle.setSuffix(" °"); self.offset_angle.setDecimals(1)
        self.offset_x.setSingleStep(5); self.offset_y.setSingleStep(5); self.offset_angle.setSingleStep(1.0)
        self.offset_x.setButtonSymbols(QSpinBox.ButtonSymbols.UpDownArrows)
        self.offset_y.setButtonSymbols(QSpinBox.ButtonSymbols.UpDownArrows)
        self.offset_angle.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.UpDownArrows)
        for widget in (
            original_host, confidence_host,
            self.font, self.font_size, self.alignment, self.bubble_type,
            self.color, self.gradient_enabled, self.gradient_start,
            self.gradient_end, self.gradient_angle, self.offset_x, self.offset_y, self.offset_angle,
        ):
            self._make_inspector_field_responsive(widget)
        self._make_inspector_field_responsive(self.translation, translation_field_min_width)
        for label, widget in (("Original", original_host), ("Translation", self.translation), ("Confidence", confidence_host)): form.addRow(label, widget)
        form.addRow(self.replace)
        editor_layout.addWidget(self._build_static_inspector_section("1. Translation", form_host))
        editor_layout.addWidget(self._build_inspector_section("2. Region", (("Region type", self.bubble_type), ("Alignment", self.alignment)), expanded=True))
        editor_layout.addWidget(self._build_inspector_section("3. Typography", (("Font", self.font), ("Size", self.font_size)), expanded=False))
        editor_layout.addWidget(self._build_inspector_section("4. Transform", (("X", self.offset_x), ("Y", self.offset_y), ("Rotation", self.offset_angle)), expanded=False))
        editor_layout.addWidget(self._build_inspector_section(
            "5. Appearance",
            (
                ("Color", self.color),
                ("Gradient", self.gradient_enabled),
                ("Start", self.gradient_start),
                ("End", self.gradient_end),
                ("Angle", self.gradient_angle),
            ),
            expanded=False,
        ))
        editor_layout.addStretch()
        form_scroll = QScrollArea(); form_scroll.setWidgetResizable(True); form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); form_scroll.setWidget(editor_host)
        text_layout.addWidget(form_scroll, 1)
        footer = QFrame(); footer.setObjectName("InspectorFooter")
        footer_layout = QVBoxLayout(footer); footer_layout.setContentsMargins(0, 7, 0, 0); footer_layout.setSpacing(6)
        actions = QGridLayout(); actions.setHorizontalSpacing(6); actions.setVerticalSpacing(6)
        self.remove_block = QPushButton("Remove Block"); self.remove_block.setObjectName("DangerButton"); self.remove_block.clicked.connect(self._remove_selected_block); self.remove_block.setEnabled(False)
        self.remove_block.setToolTip("Remove the selected automatic block, or delete the selected manual block")
        self.restore_auto = QPushButton("Restore Auto"); self.restore_auto.clicked.connect(self._restore_auto_blocks); self.restore_auto.setEnabled(False)
        self.restore_auto.setToolTip("Restore automatic blocks that were removed from this page")
        self.apply_button = QPushButton("Apply && Rerender"); self.apply_button.setObjectName("InspectorPrimary"); self.apply_button.clicked.connect(self._apply)
        self.apply_button.setToolTip("Save these text settings and rebuild the translated page")
        self.reset_button = QPushButton("Reset"); self.reset_button.clicked.connect(self._reset_edit)
        self.reset_button.setToolTip("Reset this block to its automatic text settings")
        self.approve_block = QPushButton("Approve Bubble"); self.approve_block.setToolTip("Approve this bubble for learning"); self.approve_block.clicked.connect(self._approve_ai_block)
        self.approve_page_bubbles = QPushButton("Approve Page OCR"); self.approve_page_bubbles.setToolTip("Approve all OCR/bubble issues on this page for learning"); self.approve_page_bubbles.clicked.connect(self._approve_ai_page_bubbles)
        self.approve_page_reviews = QPushButton("Approve Page Review"); self.approve_page_reviews.setToolTip("Approve all non-OCR review issues on this page for learning"); self.approve_page_reviews.clicked.connect(self._approve_ai_page_reviews)
        actions.addWidget(self.apply_button, 0, 0, 1, 2)
        actions.addWidget(self.remove_block, 1, 0); actions.addWidget(self.restore_auto, 1, 1)
        actions.addWidget(self.reset_button, 2, 0, 1, 2)
        actions.addWidget(self.approve_block, 3, 0, 1, 2)
        actions.addWidget(self.approve_page_bubbles, 4, 0); actions.addWidget(self.approve_page_reviews, 4, 1)
        footer_layout.addLayout(actions)
        text_layout.addWidget(footer)
        tabs.addTab(text_tab, "Text Blocks")
        self._update_font_preview(self.font.currentText()); self._update_color_swatch("#111111")
        self._update_gradient_start_swatch("#111111"); self._update_gradient_end_swatch("#ffffff")
        self._update_gradient_controls_enabled(False)
        info_tab = QWidget(); info_layout = QFormLayout(info_tab)
        self.info_path = QLabel("—"); self.info_path.setWordWrap(True); self.info_language = QLabel("—"); self.info_status = QLabel("—")
        info_layout.addRow("Source", self.info_path); info_layout.addRow("Language", self.info_language); info_layout.addRow("Status", self.info_status); tabs.addTab(info_tab, "Image Info")
        return frame

    def _build_static_inspector_section(self, title: str, content: QWidget) -> QFrame:
        section = QFrame(self)
        section.setObjectName("InspectorSection")
        section.setMinimumWidth(0)
        section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(section)
        layout.setContentsMargins(10, 7, 10, 8)
        layout.setSpacing(6)
        heading = QLabel(title)
        heading.setObjectName("InspectorSectionTitle")
        layout.addWidget(heading)
        layout.addWidget(content)
        return section

    @staticmethod
    def _make_inspector_field_responsive(widget: QWidget, minimum_width: int = 0) -> None:
        widget.setMinimumWidth(minimum_width)
        policy = widget.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
        widget.setSizePolicy(policy)

    def _build_inspector_section(self, title: str, rows: tuple[tuple[str, QWidget], ...], *, expanded: bool = False) -> CollapsibleSection:
        section = CollapsibleSection(title, expanded, self)
        section.setMinimumWidth(0)
        section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        section.body.setMinimumWidth(0)
        section.body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        form = QFormLayout(section.body)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setContentsMargins(10, 2, 10, 9)
        form.setHorizontalSpacing(8); form.setVerticalSpacing(7)
        for label, widget in rows:
            self._make_inspector_field_responsive(widget)
            form.addRow(label, widget)
        return section

    def _sync_bubble_type_options(self, current_type: str) -> None:
        current_type = "title" if current_type == "title" else normalize_region_type(current_type or "dialogue")
        self.bubble_type.blockSignals(True)
        self.bubble_type.clear()
        options = [
            ("Dialogue", "dialogue"),
            ("Title", "title"),
            ("SFX", "sfx"),
            ("Sign", "sign"),
            ("Credit", "credit"),
        ]
        for label, value in options:
            self.bubble_type.addItem(label, value)
        index = self.bubble_type.findData(current_type)
        self.bubble_type.setCurrentIndex(max(0, index))
        self.bubble_type.blockSignals(False)

    def _current_region_type(self) -> str:
        return normalize_region_type(self.bubble_type.currentData() or "dialogue")

    def _current_region_uses_art_appearance(self) -> bool:
        return self._current_region_type() in self._ART_APPEARANCE_TYPES

    def _refresh_appearance_for_selected_type(self) -> None:
        self._update_gradient_controls_enabled(self.gradient_enabled.isChecked())

    @staticmethod
    def _rgb_to_hex(value: object) -> str | None:
        if isinstance(value, str):
            color = QColor(value)
            return color.name() if color.isValid() else None
        if not isinstance(value, (list, tuple)) or len(value) < 3:
            return None
        try:
            red, green, blue = (max(0, min(255, int(round(float(item))))) for item in value[:3])
        except (TypeError, ValueError):
            return None
        return f"#{red:02x}{green:02x}{blue:02x}"

    @staticmethod
    def _hex_to_rgb(value: str) -> list[int]:
        color = QColor(value)
        if not color.isValid():
            color = QColor("#111111")
        return [color.red(), color.green(), color.blue()]

    @classmethod
    def _first_source_color(cls, group: dict) -> str | None:
        colors = group.get("source_text_colors")
        if not isinstance(colors, list):
            return None
        for color in colors:
            hex_color = cls._rgb_to_hex(color)
            if hex_color:
                return hex_color
        return None

    @classmethod
    def _profile_fill_color(cls, profile: dict | None) -> str | None:
        if not isinstance(profile, dict):
            return None
        fill = profile.get("fill")
        if not isinstance(fill, dict):
            return None
        return cls._rgb_to_hex(fill.get("dominant_color") or fill.get("average_color"))

    @classmethod
    def _profile_gradient_colors(cls, profile: dict | None) -> tuple[str, str, int] | None:
        if not isinstance(profile, dict):
            return None
        gradient = profile.get("gradient")
        if not isinstance(gradient, dict) or gradient.get("kind") != "linear":
            return None
        colors = gradient.get("colors")
        if not isinstance(colors, list) or len(colors) < 2:
            return None
        start = cls._rgb_to_hex(colors[0])
        end = cls._rgb_to_hex(colors[1])
        if not start or not end:
            return None
        try:
            angle = int(round(float(gradient.get("angle", 90))))
        except (TypeError, ValueError):
            angle = 90
        return start, end, max(0, min(180, angle))

    def _load_appearance_controls(self, group: dict, edit: RegionEdit) -> None:
        profile = edit.style_profile if isinstance(edit.style_profile, dict) else group.get("style_profile")
        color = (
            self._profile_fill_color(profile)
            or self._first_source_color(group)
            or self._rgb_to_hex(edit.color)
            or "#111111"
        )
        self._update_color_swatch(color)
        gradient = self._profile_gradient_colors(profile)
        if gradient is not None:
            start, end, angle = gradient
        else:
            start, end, angle = color, "#ffffff", 90
        self._update_gradient_start_swatch(start)
        self._update_gradient_end_swatch(end)
        self.gradient_angle.setValue(angle)
        self.gradient_enabled.blockSignals(True)
        self.gradient_enabled.setChecked(gradient is not None and self._current_region_uses_art_appearance())
        self.gradient_enabled.blockSignals(False)
        self._update_gradient_controls_enabled(self.gradient_enabled.isChecked())

    def _style_profile_from_appearance(self, group: dict, existing: RegionEdit) -> dict | None:
        if not self._current_region_uses_art_appearance():
            return None
        source = existing.style_profile if isinstance(existing.style_profile, dict) else group.get("style_profile")
        profile = dict(source) if isinstance(source, dict) else {}
        fill = dict(profile.get("fill")) if isinstance(profile.get("fill"), dict) else {}
        fill_color = self._hex_to_rgb(self.color.text())
        fill.update({
            "dominant_color": fill_color,
            "average_color": fill_color,
            "colors": [fill_color],
        })
        try:
            profile["version"] = max(2, int(profile.get("version", 2) or 2))
        except (TypeError, ValueError):
            profile["version"] = 2
        profile["fill"] = fill
        if self.gradient_enabled.isChecked():
            start = self._hex_to_rgb(self.gradient_start.text())
            end = self._hex_to_rgb(self.gradient_end.text())
            profile["gradient"] = {
                "kind": "linear",
                "colors": [start, end],
                "angle": float(self.gradient_angle.value()),
            }
        else:
            profile["gradient"] = None
        return profile

    def _build_offset_control(self, spinbox: QSpinBox, negative_label: str, positive_label: str) -> QWidget:
        host = QWidget()
        layout = QHBoxLayout(host); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(4)
        negative = QPushButton("-"); negative.setFixedWidth(30); negative.setToolTip(f"Nudge {negative_label}")
        positive = QPushButton("+"); positive.setFixedWidth(30); positive.setToolTip(f"Nudge {positive_label}")
        negative.clicked.connect(lambda: spinbox.setValue(spinbox.value() - spinbox.singleStep()))
        positive.clicked.connect(lambda: spinbox.setValue(spinbox.value() + spinbox.singleStep()))
        layout.addWidget(negative); layout.addWidget(spinbox, 1); layout.addWidget(positive)
        return host

    def _filmstrip_expanded_changed(self, expanded: bool) -> None:
        if hasattr(self, "filmstrip_jump_host"):
            self.filmstrip_jump_host.setVisible(expanded)
        project = WORKSPACE.current
        if self._filmstrip_collapse_mode() == "always_collapsed":
            return
        if project is None or bool(getattr(project, "filmstrip_visible", True)) == expanded:
            return
        project.filmstrip_visible = expanded
        WORKSPACE.save()

    @staticmethod
    def _filmstrip_collapse_mode() -> str:
        mode = getattr(SETTINGS, "filmstrip_collapse_mode", "current") or "current"
        if mode in {"always_collapsed", "always_expanded"}:
            return mode
        return "current"

    def _apply_filmstrip_collapse_preference(self, project, project_id: str) -> None:
        mode = self._filmstrip_collapse_mode()
        project_changed = project_id != self._filmstrip_policy_project_id
        mode_changed = mode != self._filmstrip_policy_mode
        if mode == "always_collapsed":
            if project_changed or mode_changed:
                self.filmstrip_section.set_expanded(False)
                self.filmstrip_jump_host.setVisible(False)
        elif mode == "always_expanded":
            if project_changed or mode_changed:
                self.filmstrip_section.set_expanded(True)
                self.filmstrip_jump_host.setVisible(True)
        else:
            # "current" — respect the per-project filmstrip_visible flag
            expanded = bool(getattr(project, "filmstrip_visible", True))
            self.filmstrip_section.set_expanded(expanded)
            self.filmstrip_jump_host.setVisible(expanded)
        self._filmstrip_policy_project_id = project_id
        self._filmstrip_policy_mode = mode

    def _show_identity_preview(self) -> None:
        if self.identity_thumbnail_path is None:
            return
        IdentityPreviewDialog(self.identity_thumbnail_path, self).exec()
        if self.filmstrip.currentRow() >= 0:
            self.filmstrip.setFocus()

    def _identity_tile_menu(self, position) -> None:
        if self.identity_thumbnail_path is None:
            return
        menu = QMenu(self)
        set_thumbnail = menu.addAction("Set as Recent Thumbnail")
        set_thumbnail.setIcon(lucide_icon("image-plus"))
        set_thumbnail.setEnabled(WORKSPACE.current is not None and not APP_STATE.busy)
        set_thumbnail.triggered.connect(self._set_identity_recent_thumbnail)
        menu.exec(self.identity_tile.mapToGlobal(position))

    def refresh(self, project, *, force_filmstrip_rebuild: bool = False) -> None:
        if project is None:
            self._layout_undo.clear()
            self._layout_redo.clear()
            self._filmstrip_undo.clear()
            self._filmstrip_redo.clear()
            self._pending_text_layouts.clear()
            self._pending_manual_history.clear()
            self._reset_project_view_state()
            return
        self._project_title_full = project.name; self.project_title.setToolTip(project.name); self._update_project_title()
        self.count_label.setText(f"{len(project.images)} images")
        page_count = len(project.images)
        self.filmstrip_section.toggle.setText(
            f"Filmstrip • {page_count} page{'s' if page_count != 1 else ''}"
        )
        self.filmstrip_jump.setEnabled(page_count > 0)
        self.filmstrip_jump_button.setEnabled(page_count > 0)
        self.start_button.setEnabled(not APP_STATE.busy and any(image.status in TRANSLATE_ELIGIBLE_STATUSES for image in project.images))
        self.quality_combo.setCurrentText(project.quality)
        self.style_combo.setCurrentText(project.text_style)
        source_index = self.source_combo.findData(project.source_language); self.source_combo.setCurrentIndex(max(0, source_index))
        current = (
            max(0, min(APP_STATE.selected_image, len(project.images) - 1))
            if project.images and APP_STATE.selected_image >= 0 else -1
        )
        self._sync_filmstrip_jump(current, page_count)
        image_ids = [self._image_id(image) for image in project.images]
        project_id = str(getattr(project, "id", project.name))
        self._apply_filmstrip_collapse_preference(project, project_id)
        live_items = self._current_filmstrip_items()
        project_changed = project_id != self._filmstrip_project_id
        if project_changed:
            self._layout_undo.clear()
            self._layout_redo.clear()
            self._filmstrip_undo.clear()
            self._filmstrip_redo.clear()
            self._pending_text_layouts.clear()
            self._pending_manual_history.clear()
        show_identity_on_open = project_changed and self.identity_thumbnail_path is not None
        identity_active = self._identity_workspace_active()
        if identity_active:
            current = -1
        filmstrip_current = -1 if show_identity_on_open else current
        if force_filmstrip_rebuild or project_changed or image_ids != list(live_items):
            self._rebuild_filmstrip(project, project_id, image_ids, filmstrip_current)
            if show_identity_on_open:
                self._select_identity()
                # Identity view is now active; do NOT fall through to _load_image()
                return
        else:
            self._filmstrip_items = live_items
            for image_index, image in enumerate(project.images):
                self._update_filmstrip_item(self._filmstrip_items[image_ids[image_index]], image, image_index)
            if current >= 0 and self.filmstrip.currentRow() < 0:
                self.filmstrip.setCurrentRow(current)
            elif current < 0:
                self._clear_filmstrip_current()
        if identity_active:
            APP_STATE.selected_image = -1
            APP_STATE.selected_block = -1
            if getattr(project, "selected_image", -1) != -1:
                project.selected_image = -1
                WORKSPACE.save()
            self._clear_filmstrip_current()
            self._show_identity_workspace()
            return
        selected_image = APP_STATE.selected_image
        if selected_image >= 0:
            selected_image = max(0, min(selected_image, len(project.images) - 1))
            self._load_image(selected_image, APP_STATE.selected_block)
        else:
            self._show_identity_workspace()

    def _reset_project_view_state(self) -> None:
        self.stop_thumbnail_loading()
        self._filmstrip_project_id = ""
        self._filmstrip_items = {}
        self._ignore_next_open_page_selection = False
        self.identity_tile.setChecked(False)
        self.filmstrip.blockSignals(True)
        self.filmstrip.clearSelection()
        self.filmstrip.clear()
        self.filmstrip.blockSignals(False)
        self.filmstrip_jump.clear()
        self.filmstrip_jump.setEnabled(False)
        self.filmstrip_jump_button.setEnabled(False)
        self._groups = []
        if hasattr(self, "canvas_stack"):
            self.canvas_stack.setCurrentWidget(self.page_canvases)
        if hasattr(self, "original_status"):
            self._update_canvas_status("ready")

    def _identity_workspace_active(self) -> bool:
        return (
            self.identity_thumbnail_path is not None
            and self.identity_tile.isChecked()
            and self.filmstrip.currentRow() < 0
            and self.canvas_stack.currentWidget() is self.identity_preview
        )

    def _show_identity_tile_in_filmstrip(self) -> None:
        self.filmstrip.horizontalScrollBar().setValue(0)
        self.identity_tile.raise_()
        QTimer.singleShot(0, lambda: self.filmstrip.horizontalScrollBar().setValue(0))

    def _select_identity(self) -> None:
        if self.identity_thumbnail_path is None:
            return
        self.identity_tile.setChecked(True)
        self.filmstrip.blockSignals(True)
        self._clear_filmstrip_current()
        self.filmstrip.blockSignals(False)
        self._selection_changed()
        if APP_STATE.selected_image != -1 or APP_STATE.selected_block != -1:
            APP_STATE.select(-1, -1)
        # Absorb 2 incoming selection_changed signals:
        # 1) set_project's own selection_changed.emit at end of set_project()
        # 2) _set_current's APP_STATE.select(last_page) call after set_project returns
        self._ignore_next_open_page_selection = 2
        self._show_identity_workspace()
        self._show_identity_tile_in_filmstrip()

    def _show_identity_workspace(self) -> None:
        if self.identity_thumbnail_path is None:
            return
        self.image_label.setText("Hydra identity")
        self.selection_label.setText("Hydra selected")
        self.info_path.setText(str(self.identity_thumbnail_path))
        self.info_language.setText("Brand identity")
        self.info_status.setText("Preview")
        self.original.set_badge("Hydra Identity")
        self.translated.set_badge("Hydra Identity")
        self.identity_preview.set_badge("Hydra Identity")
        self._update_canvas_status("Preview")
        self.canvas_stack.setCurrentWidget(self.identity_preview)
        self._groups = []
        self.speech.stop()
        self.speak_original.setEnabled(False)
        self.remove_block.setEnabled(False)
        self.restore_auto.setEnabled(False)
        self.add_box.setEnabled(False)
        self.title_reconstruction.setEnabled(False)
        self.blocks.blockSignals(True)
        self.blocks.clear()
        self.blocks.blockSignals(False)
        self.original_text.clear()
        self.translation.clear()
        self.confidence.setText("—")
        self.confidence_bar.setValue(0)
        self.original.set_content(None, [], -1)
        self.translated.set_content(None, [], -1)
        self.identity_preview.set_content(self.identity_thumbnail_path, [], -1)
        # Defer fit so Qt has finalized canvas_stack geometry after setCurrentWidget
        QTimer.singleShot(0, self.identity_preview.fit_image)

    def _clear_filmstrip_current(self) -> None:
        self.filmstrip.clearSelection()
        self.filmstrip.setCurrentIndex(QModelIndex())
        selection_model = self.filmstrip.selectionModel()
        if selection_model is not None:
            selection_model.clearCurrentIndex()

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

        add_item = QListWidgetItem()
        add_item.setData(Qt.ItemDataRole.UserRole, "__add_pages__")
        add_item.setSizeHint(FILMSTRIP_CARD_SIZE)
        add_item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
        add_item.setIcon(self.filmstrip.create_add_pages_icon())
        add_item.setText("Add Pages")
        add_item.setToolTip("Click to import additional manga pages or folders into this project")
        add_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.filmstrip.addItem(add_item)

        self._filmstrip_project_id = project_id
        if current >= 0:
            self.filmstrip.setCurrentRow(current)
            self.filmstrip.clearSelection()
        for image_id in selected_ids:
            if image_id in self._filmstrip_items:
                self._filmstrip_items[image_id].setSelected(True)
        self.filmstrip.blockSignals(False); self._selection_changed()
        if thumbnail_inputs:
            self._start_thumbnail_loading(project_id, thumbnail_inputs)

    def _current_filmstrip_items(self) -> dict[str, QListWidgetItem]:
        return {
            str(self.filmstrip.item(row).data(Qt.ItemDataRole.UserRole)): self.filmstrip.item(row)
            for row in range(self.filmstrip.count())
            if self.filmstrip.item(row) is not None and str(self.filmstrip.item(row).data(Qt.ItemDataRole.UserRole) or "") != "__add_pages__"
        }

    def _on_add_pages_clicked(self) -> None:
        if APP_STATE.busy:
            QMessageBox.information(
                self,
                "Translation Active",
                "Please wait for active translation tasks to finish before adding new pages."
            )
            return
        menu = QMenu(self)
        action_images = menu.addAction("Add Image File(s)...")
        action_folder = menu.addAction("Add Image Folder...")
        chosen = menu.exec(QCursor.pos())
        from hydra_manga_tl.ui.landing import configured_manga_import_root
        if chosen == action_images:
            files, _ = QFileDialog.getOpenFileNames(
                self,
                "Add manga images",
                "",
                "Images (*.jpg *.jpeg *.png *.webp *.tif *.tiff *.bmp)"
            )
            if files:
                self._append_input_paths([Path(p) for p in files])
        elif chosen == action_folder:
            folder = QFileDialog.getExistingDirectory(
                self,
                "Add manga folder",
                str(configured_manga_import_root()),
                QFileDialog.Option.ShowDirsOnly,
            )
            if folder:
                self._append_input_paths([Path(folder)])

    def _append_input_paths(self, paths: list[Path]) -> None:
        if not paths or WORKSPACE.current is None:
            return
        added = WORKSPACE.add_inputs(paths)
        if added > 0:
            self.status.setText(f"Added {added} page{'s' if added != 1 else ''} to project.")

    @staticmethod
    def _update_filmstrip_item(item: QListWidgetItem, image, image_index: int) -> None:
        item.setText(_page_label(image.source_path, image_index))
        item.setForeground(QColor({"ready":"#66d69a", "review":"#ffcc66", "failed":"#ff6b73", "ocr":"#69a0ff", "translating":"#69a0ff", "reconstructing":"#69a0ff"}.get(image.status, "#d7deea")))
        details = f"{Path(image.source_path).name}\n{image.source_path}\nStatus: {image.status}"
        if image.error: details += f"\n{pipeline_error(image.error)}"
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

    def _queue_thumbnail_loading(self, project_id: str, images: list[tuple[str, str]]) -> None:
        self._start_thumbnail_loading(project_id, images)

    def _apply_thumbnail(self, project_id: str, image_id: str, image) -> None:
        if project_id != self._filmstrip_project_id:
            return
        self._filmstrip_items = self._current_filmstrip_items()
        item = self._filmstrip_items.get(image_id)
        if item is None:
            return
        item.setIcon(self._thumbnail_icon(image))

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
            item = self.filmstrip.item(row)
            image_id = str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""
            if image_id == "__add_pages__":
                return
            self._ignore_next_open_page_selection = 0
            self.identity_tile.setChecked(False)
            APP_STATE.select(row)

    def _sync_filmstrip_jump(self, index: int, total: int | None = None) -> None:
        if not hasattr(self, "filmstrip_jump"):
            return
        page_count = total
        if page_count is None:
            page_count = len(WORKSPACE.current.images) if WORKSPACE.current is not None else 0
        enabled = page_count > 0
        self.filmstrip_jump.setEnabled(enabled)
        self.filmstrip_jump_button.setEnabled(enabled)
        self.filmstrip_jump.setText(str(index + 1) if enabled and 0 <= index < page_count else "")

    def _jump_to_filmstrip_page(self) -> None:
        project = WORKSPACE.current
        text = self.filmstrip_jump.text().strip()
        if project is None or not project.images:
            self.status.setText("No pages are available.")
            return
        if not text:
            self.status.setText("Enter a page number to jump.")
            return
        try:
            page_number = int(text)
        except ValueError:
            self.status.setText("Enter a valid page number.")
            return
        if not (1 <= page_number <= len(project.images)):
            self.status.setText(f"Page {page_number} is not available.")
            return
        index = page_number - 1
        self.identity_tile.setChecked(False)
        APP_STATE.select(index, -1)
        item = self.filmstrip.item(index)
        if item is not None:
            self.filmstrip.scrollToItem(item, QAbstractItemView.ScrollHint.EnsureVisible)
        self.status.setText(f"Jumped to page {page_number}.")

    def _on_filmstrip_reordered(self, ordered_ids: list[str]) -> None:
        selected_ids = {
            str(item.data(Qt.ItemDataRole.UserRole)) for item in self.filmstrip.selectedItems()
            if str(item.data(Qt.ItemDataRole.UserRole) or "") != "__add_pages__"
        } | set(getattr(self.filmstrip, "_last_moved_ids", set()))
        before_ids = [image.id for image in WORKSPACE.current.images] if WORKSPACE.current is not None else []
        moved_count = len(selected_ids) or 1
        moved_id = next((image_id for image_id in ordered_ids if image_id in selected_ids), ordered_ids[0] if ordered_ids else "")
        from_position = before_ids.index(moved_id) + 1 if moved_id in before_ids else 0
        to_position = ordered_ids.index(moved_id) + 1 if moved_id in ordered_ids else 0
        changed_ids = {
            image_id for index, image_id in enumerate(ordered_ids)
            if index >= len(before_ids) or before_ids[index] != image_id
        }
        self._filmstrip_items = self._current_filmstrip_items()
        if not WORKSPACE.reorder_images(ordered_ids):
            self._filmstrip_project_id = ""
            if WORKSPACE.current is not None:
                self.refresh(WORKSPACE.current)
            return
        if before_ids != ordered_ids:
            self._filmstrip_undo.append({
                "kind": "filmstrip_reorder",
                "before": list(before_ids),
                "after": list(ordered_ids),
                "selected_ids": sorted(selected_ids),
                "moved_count": moved_count,
            })
            del self._filmstrip_undo[:-200]
            self._filmstrip_redo.clear()
        if WORKSPACE.current is not None:
            self.refresh(WORKSPACE.current, force_filmstrip_rebuild=True)
        self.filmstrip.blockSignals(True); self.filmstrip.clearSelection()
        first_selected = next((self._filmstrip_items[image_id] for image_id in ordered_ids if image_id in selected_ids and image_id in self._filmstrip_items), None)
        if first_selected is not None:
            self.filmstrip.setCurrentItem(first_selected)
            self.filmstrip.scrollToItem(first_selected, QAbstractItemView.ScrollHint.PositionAtCenter)
        for image_id in selected_ids:
            if image_id in self._filmstrip_items:
                self._filmstrip_items[image_id].setSelected(True)
        self.filmstrip.blockSignals(False); self._selection_changed()
        self._flash_filmstrip_items(changed_ids or selected_ids)
        if from_position and to_position:
            message = (
                f"Page moved: Page {from_position} -> Position {to_position}"
                if moved_count == 1 else f"Moved {moved_count} pages -> Position {to_position}"
            )
            self.status.setText(message)
            QTimer.singleShot(2500, lambda text=message: self.status.setText("Ready") if self.status.text() == text else None)

    def _focus_in_filmstrip(self) -> bool:
        focus = QApplication.focusWidget()
        return focus is self.filmstrip or (
            focus is not None and self.filmstrip.isAncestorOf(focus)
        )

    def _apply_filmstrip_history_command(self, command: dict, state_key: str) -> bool:
        if command.get("kind") != "filmstrip_reorder" or WORKSPACE.current is None:
            return False
        ordered_ids = [str(image_id) for image_id in command.get(state_key, [])]
        current_ids = [image.id for image in WORKSPACE.current.images]
        if set(ordered_ids) != set(current_ids) or len(ordered_ids) != len(current_ids):
            self.status.setText("Filmstrip history no longer matches this project")
            return False
        if ordered_ids == current_ids:
            return True
        if not WORKSPACE.reorder_images(ordered_ids):
            self.status.setText("Could not restore filmstrip order")
            return False
        self.refresh(WORKSPACE.current, force_filmstrip_rebuild=True)
        selected_ids = {
            str(image_id)
            for image_id in command.get("selected_ids", [])
            if str(image_id) in self._filmstrip_items
        }
        self.filmstrip.blockSignals(True)
        self.filmstrip.clearSelection()
        first_selected = None
        for image_id in ordered_ids:
            item = self._filmstrip_items.get(image_id)
            if item is None:
                continue
            if image_id in selected_ids:
                item.setSelected(True)
                first_selected = first_selected or item
        if first_selected is not None:
            self.filmstrip.setCurrentItem(first_selected)
            self.filmstrip.scrollToItem(first_selected, QAbstractItemView.ScrollHint.PositionAtCenter)
        self.filmstrip.blockSignals(False)
        self._selection_changed()
        self._flash_filmstrip_items(selected_ids)
        action = "Undid" if state_key == "before" else "Redid"
        count = int(command.get("moved_count", len(selected_ids) or 1) or 1)
        self.status.setText(f"{action} filmstrip reorder ({count} page{'s' if count != 1 else ''})")
        return True

    def _undo_workspace(self) -> None:
        if self._focus_in_filmstrip():
            if not self._filmstrip_undo:
                return
            command = self._filmstrip_undo.pop()
            if self._apply_filmstrip_history_command(command, "before"):
                self._filmstrip_redo.append(command)
            return
        self._undo_text_layout()

    def _redo_workspace(self) -> None:
        if self._focus_in_filmstrip():
            if not self._filmstrip_redo:
                return
            command = self._filmstrip_redo.pop()
            if self._apply_filmstrip_history_command(command, "after"):
                self._filmstrip_undo.append(command)
            return
        self._redo_text_layout()

    def _flash_filmstrip_items(self, image_ids: set[str]) -> None:
        if not image_ids:
            return
        original_backgrounds = {}
        for image_id in image_ids:
            item = self._filmstrip_items.get(image_id)
            if item is None:
                continue
            original_backgrounds[image_id] = item.background()
            item.setBackground(QColor("#24486f"))
        QTimer.singleShot(420, lambda: self._restore_filmstrip_backgrounds(original_backgrounds))

    def _restore_filmstrip_backgrounds(self, backgrounds) -> None:
        for image_id, background in backgrounds.items():
            item = self._filmstrip_items.get(image_id)
            if item is not None:
                item.setBackground(background)

    def _selection_changed(self) -> None:
        count = len(self.filmstrip.selectedItems())
        self.selection_label.setText(f"{count} selected")
        eligible = self._selected_image_ids(eligible_only=True)
        self._set_responsive_button_text(
            self.selected_button,
            f"Translate Selected ({len(eligible)})" if eligible else "Translate Selected",
        )
        self.selected_button.setEnabled(bool(eligible) and not APP_STATE.busy)
        if hasattr(self, "add_box"):
            self.add_box.setEnabled(not self._manual_busy)
        if hasattr(self, "title_reconstruction"):
            self.title_reconstruction.setEnabled(not self._manual_busy)

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
        retranslate = menu.addAction(f"Retranslate Selected ({len(all_selected)})")
        retranslate.setEnabled(bool(all_selected) and not APP_STATE.busy)
        retranslate.triggered.connect(lambda: self._retranslate_selected(all_selected))
        menu.addSeparator()
        thumbnail_image_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        set_thumbnail = menu.addAction("Set as Recent Thumbnail")
        set_thumbnail.setIcon(lucide_icon("image-plus"))
        set_thumbnail.setEnabled(bool(thumbnail_image_id) and WORKSPACE.current is not None and not APP_STATE.busy)
        set_thumbnail.triggered.connect(lambda: self._set_recent_thumbnail(thumbnail_image_id))
        menu.addSeparator()
        delete_label = "Delete Image" if len(all_selected) == 1 else f"Delete Images ({len(all_selected)})"
        delete_pages = menu.addAction(delete_label)
        delete_pages.setEnabled(bool(all_selected) and not APP_STATE.busy)
        delete_pages.triggered.connect(lambda: self._delete_selected_images(all_selected))
        menu.exec(self.filmstrip.viewport().mapToGlobal(position))

    def _selected_image_ids(self, eligible_only: bool = False) -> set[str]:
        selected = {str(item.data(Qt.ItemDataRole.UserRole)) for item in self.filmstrip.selectedItems()}
        if not eligible_only or WORKSPACE.current is None:
            return selected
        return {
            image.id for image in WORKSPACE.current.images
            if image.id in selected and image.status in TRANSLATE_ELIGIBLE_STATUSES
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
        if confirm(
            self, "Retranslate selected pages",
            "Remove existing OCR, automatic translation, and rendered output for the selected pages, then run OCR, translation, and rendering again? Manual boxes and edits will be kept.",
        ):
            WORKSPACE.start_pipeline(image_ids, retranslate=True)

    def _set_recent_thumbnail(self, image_id: str) -> None:
        try:
            thumbnail = WORKSPACE.set_recent_thumbnail(image_id)
        except ValueError as error:
            QMessageBox.warning(
                self,
                "Could not set thumbnail",
                workspace_action_error(error, action="set recent project thumbnail"),
            )
            return
        self.status.setText(f"Recent project thumbnail set to {thumbnail.name}")

    def _set_identity_recent_thumbnail(self) -> None:
        if self.identity_thumbnail_path is None:
            return
        try:
            thumbnail = WORKSPACE.set_recent_thumbnail_path(self.identity_thumbnail_path)
        except ValueError as error:
            QMessageBox.warning(
                self,
                "Could not set thumbnail",
                workspace_action_error(error, action="set Hydra identity as recent project thumbnail"),
            )
            return
        self.status.setText(f"Recent project thumbnail set to Hydra identity ({thumbnail.name})")

    def _delete_selected_images(self, image_ids: set[str] | None = None) -> None:
        selected_ids = set(image_ids or self._selected_image_ids(eligible_only=False))
        if not selected_ids or WORKSPACE.current is None:
            return
        count = len(selected_ids)
        noun = "image" if count == 1 else "images"
        answer = QMessageBox.question(
            self,
            f"Delete {noun}",
            f"Remove {count} selected {noun} from this project?\n\n"
            "The original image files will remain on disk. Remaining pages will be renumbered automatically.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = WORKSPACE.remove_images(selected_ids)
        except ValueError as error:
            QMessageBox.warning(
                self,
                f"Could not delete {noun}",
                workspace_action_error(error, action=f"delete selected {noun}"),
            )
            return
        if not removed:
            QMessageBox.warning(
                self,
                f"Could not delete {noun}",
                "The selected images could not be removed while translation is running.",
            )
            return
        message = (
            f"{removed} image was removed from the project."
            if removed == 1
            else f"{removed} images were removed from the project."
        )
        self.status.setText(f"{message} Remaining pages were renumbered.")
        QMessageBox.information(
            self,
            "Images deleted",
            f"{message}\n\nThe original files were not deleted.",
        )

    def _load_image(self, index: int, block: int = -1) -> None:
        project = WORKSPACE.current
        if project is None or not (0 <= index < len(project.images)): return
        self.identity_tile.setChecked(False)
        self.canvas_stack.setCurrentWidget(self.page_canvases)
        self._sync_filmstrip_jump(index, len(project.images))
        image = project.images[index]; self.image_label.setText(f"Page {index + 1} of {len(project.images)}")
        self.info_path.setText(image.source_path); self.info_language.setText(image.source_language or "Not analyzed"); self.info_status.setText(image.status)
        self._update_canvas_status(image.status)
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
        for group in self._groups:
            pending = self._pending_text_layouts.get((index, str(group.get("index"))))
            if pending is not None:
                group["text_layout"] = dict(pending)
        self.blocks.blockSignals(True); self.blocks.clear()
        for block_row, group in enumerate(self._groups, 1):
            ocr_reasons = WORKSPACE.ocr_review_reasons(group)
            item = QListWidgetItem(self._block_list_label(group, block_row, bool(ocr_reasons)))
            tooltip = group["original_text"]
            if ocr_reasons:
                tooltip = f"{tooltip}\nOCR review: {', '.join(ocr_reasons)}"
                item.setForeground(QColor("#ffcc66"))
            item.setToolTip(tooltip); self.blocks.addItem(item)
        self.blocks.setCurrentRow(block); self.blocks.blockSignals(False)
        self._update_ocr_queue_status()
        final = Path(image.rendered_image) if image.rendered_image else None
        self.original.set_content(Path(image.source_path), self._groups, block)
        self.translated.set_content(final, self._groups, block)
        if block >= 0: self._load_block(block)

    def _load_block(self, row: int) -> None:
        if not (0 <= row < len(self._groups)): return
        group = self._groups[row]; image = WORKSPACE.current.images[APP_STATE.selected_image]
        edit = image.edits.get(str(group["index"]), RegionEdit())
        self.original_text.setPlainText(group["original_text"]); self.translation.setPlainText(group["translated_text"])
        self._update_confidence_display(group)
        self.speak_original.setEnabled(bool(group.get("original_text")))
        self.remove_block.setEnabled(True)
        self.remove_block.setText("Delete Manual" if group.get("manual") else "Remove Auto")
        self.replace.setChecked(edit.replace); self.font.setCurrentText(edit.font_family); self.font_size.setValue(edit.font_size)
        alignment_index = self.alignment.findData(edit.alignment); self.alignment.setCurrentIndex(max(0, alignment_index))
        selected_type = edit.bubble_type or group.get("bubble_type", "dialogue")
        self._sync_bubble_type_options(str(selected_type))
        bubble_index = self.bubble_type.findData(selected_type); self.bubble_type.setCurrentIndex(max(0, bubble_index))
        self._load_appearance_controls(group, edit)
        self.offset_x.setValue(edit.offset_x); self.offset_y.setValue(edit.offset_y)
        self.offset_angle.setValue(edit.layout_angle or 0.0)
        self._update_ocr_queue_status()

    @staticmethod
    def _layout_from_group(group: dict) -> dict | None:
        layout = group.get("text_layout")
        if isinstance(layout, dict):
            try:
                result = {
                    "x": int(layout["x"]),
                    "y": int(layout["y"]),
                    "width": int(layout["width"]),
                    "height": int(layout["height"]),
                }
                if layout.get("angle") is not None:
                    try:
                        result["angle"] = float(layout["angle"])
                    except (TypeError, ValueError):
                        pass
                return result
            except (KeyError, TypeError, ValueError):
                pass
        rect = group.get("manual_rect")
        if isinstance(rect, list) and len(rect) == 4:
            return {
                "x": int(rect[0]), "y": int(rect[1]),
                "width": int(rect[2]) - int(rect[0]),
                "height": int(rect[3]) - int(rect[1]),
            }
        polygon = group.get("polygon", [])
        if not polygon:
            return None
        xs = [int(point[0]) for point in polygon]
        ys = [int(point[1]) for point in polygon]
        return {
            "x": min(xs), "y": min(ys),
            "width": max(xs) - min(xs),
            "height": max(ys) - min(ys),
        }

    @staticmethod
    def _layout_key(image_index: int, group_index) -> tuple[int, str]:
        return (int(image_index), str(group_index))

    def _capture_editor_history_state(self, image_index: int) -> dict | None:
        if WORKSPACE.current is None or not (0 <= image_index < len(WORKSPACE.current.images)):
            return None
        try:
            return WORKSPACE.capture_editor_state(image_index)
        except (OSError, ValueError, TypeError):
            return None

    def _push_editor_history(
        self,
        label: str,
        image_index: int,
        before: dict | None,
        after: dict | None = None,
    ) -> None:
        if before is None:
            return
        after = after if after is not None else self._capture_editor_history_state(image_index)
        if after is None or before == after:
            return
        self._layout_undo.append({
            "kind": "editor_state",
            "label": label,
            "image_index": image_index,
            "before": before,
            "after": after,
        })
        del self._layout_undo[:-200]
        self._layout_redo.clear()

    def _clear_pending_layouts_for_image(self, image_index: int) -> None:
        for key in [
            key for key in self._pending_text_layouts
            if key[0] == int(image_index)
        ]:
            self._pending_text_layouts.pop(key, None)

    def _discard_text_layout_history(self, image_index: int, group_indices: set[str]) -> None:
        def keep(command: dict) -> bool:
            return not (
                command.get("kind") == "text_layout"
                and int(command.get("image_index", -1)) == int(image_index)
                and str(command.get("group_index")) in group_indices
            )

        self._layout_undo = [command for command in self._layout_undo if keep(command)]
        self._layout_redo = [command for command in self._layout_redo if keep(command)]

    def _restore_editor_history_command(self, command: dict, state_key: str) -> bool:
        state = command.get(state_key)
        if not isinstance(state, dict):
            return False
        image_index = int(command.get("image_index", APP_STATE.selected_image))
        working = self._show_working(
            "Undo" if state_key == "before" else "Redo",
            "Restoring editor state...",
        )
        try:
            WORKSPACE.restore_editor_state(
                image_index,
                state,
                log_callback=working.append_log,
            )
            self._clear_pending_layouts_for_image(image_index)
            row = APP_STATE.selected_block if image_index == APP_STATE.selected_image else -1
            self._load_image(image_index, row)
            self.status.setText(
                f"{'Undid' if state_key == 'before' else 'Redid'} {command.get('label', 'editor change')}"
            )
            return True
        except (MemoryError, OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
            QMessageBox.warning(
                self,
                "Could not restore editor state",
                workspace_action_error(error, action="restore the editor state"),
            )
            return False
        finally:
            self._close_working(working)

    def _text_layout_changed(self, row: int, layout: dict) -> None:
        if not (0 <= row < len(self._groups)):
            return
        group = self._groups[row]
        key = self._layout_key(APP_STATE.selected_image, group["index"])
        before = self._pending_text_layouts.get(key) or self._layout_from_group(group)
        after = {
            "x": int(layout["x"]),
            "y": int(layout["y"]),
            "width": int(layout["width"]),
            "height": int(layout["height"]),
        }
        if layout.get("angle") is not None:
            try:
                after["angle"] = float(layout["angle"])
            except (TypeError, ValueError):
                pass
        if before == after:
            return
        command = {
            "kind": "text_layout",
            "image_index": APP_STATE.selected_image,
            "group_index": group["index"],
            "row": row,
            "before": before,
            "after": after,
        }
        if self._apply_text_layout_command(command, after):
            self._layout_undo.append(command)
            del self._layout_undo[:-200]
            self._layout_redo.clear()

    def _apply_text_layout_command(self, command: dict, layout: dict | None) -> bool:
        if layout is None:
            return False
        image_index = int(command["image_index"])
        group_index = command["group_index"]
        row = int(command.get("row", APP_STATE.selected_block))
        staged = {
            "x": int(layout["x"]),
            "y": int(layout["y"]),
            "width": int(layout["width"]),
            "height": int(layout["height"]),
        }
        if layout.get("angle") is not None:
            try:
                staged["angle"] = float(layout["angle"])
            except (TypeError, ValueError):
                pass
        self._pending_text_layouts[self._layout_key(image_index, group_index)] = staged
        if image_index == APP_STATE.selected_image and 0 <= row < len(self._groups):
            self._groups[row]["text_layout"] = dict(staged)
            if row == APP_STATE.selected_block:
                self.translated._set_text_layout_rect(QRectF(staged["x"], staged["y"], staged["width"], staged["height"]))
        self.status.setText("Text layout staged; click Apply & Rerender")
        return True

    @staticmethod
    def _focused_text_entry():
        focus = QApplication.focusWidget()
        return focus if isinstance(focus, (QTextEdit, QLineEdit)) else None

    def _undo_text_layout(self) -> None:
        text_entry = self._focused_text_entry()
        if text_entry is not None:
            text_entry.undo()
            return
        if not self._layout_undo:
            return
        command = self._layout_undo.pop()
        if command.get("kind") == "editor_state":
            if self._restore_editor_history_command(command, "before"):
                self._layout_redo.append(command)
            return
        if self._apply_text_layout_command(command, command.get("before")):
            self._layout_redo.append(command)

    def _redo_text_layout(self) -> None:
        text_entry = self._focused_text_entry()
        if text_entry is not None:
            text_entry.redo()
            return
        if not self._layout_redo:
            return
        command = self._layout_redo.pop()
        if command.get("kind") == "editor_state":
            if self._restore_editor_history_command(command, "after"):
                self._layout_undo.append(command)
            return
        if self._apply_text_layout_command(command, command.get("after")):
            self._layout_undo.append(command)

    @staticmethod
    def _block_list_label(group: dict, row: int, has_ocr_review: bool) -> str:
        if group.get("bubble_type") == "title":
            kind = "Title Reconstruction"
        else:
            kind = "Manual Bubble" if group.get("manual") else ("OCR Bubble" if has_ocr_review else "Bubble")
        number = row if group.get("manual") else group.get("index", row)
        snippet = str(group.get("translated_text") or group.get("original_text") or "").strip().replace("\n", " ")
        if len(snippet) > 34:
            snippet = snippet[:31].rstrip() + "..."
        return f"{kind} {number}" + (f" • {snippet}" if snippet else "")

    def _update_confidence_display(self, group: dict) -> None:
        ocr = max(0.0, min(1.0, float(group.get("ocr_confidence", 0.0) or 0.0)))
        quality = str(group.get("translation_quality", "review" if group.get("review_reasons") else "good"))
        reasons = ", ".join(str(reason) for reason in group.get("review_reasons", [])[:2])
        label = f"OCR {ocr:.0%} • Translation {quality}"
        tooltip = label
        if reasons:
            tooltip += f" • {reasons}"
        self.confidence.setText(label)
        self.confidence.setToolTip(tooltip)
        self.confidence_bar.setValue(round(ocr * 1000))

    def _select_block(self, row: int) -> None:
        if row < 0: return
        modifiers = QApplication.keyboardModifiers()
        if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return
        APP_STATE.select(APP_STATE.selected_image, row); self.blocks.blockSignals(True); self.blocks.setCurrentRow(row); self.blocks.blockSignals(False)

    def _select_relative_block(self, delta: int) -> None:
        if not self._groups:
            return
        current = APP_STATE.selected_block
        if current < 0:
            current = self.blocks.currentRow()
        target = max(0, min(len(self._groups) - 1, current + delta))
        self._select_block(target)

    def _update_ocr_queue_status(self) -> None:
        count = len(WORKSPACE.ocr_review_queue())
        self._set_responsive_button_text(
            self.next_ocr_issue,
            f"Next OCR ({count})" if count else "Next OCR",
        )
        self.next_ocr_issue.setEnabled(count > 0 and not APP_STATE.busy)
        review_count = len(WORKSPACE.review_issue_queue())
        self._set_responsive_button_text(
            self.next_review_issue,
            f"Next Review ({review_count})" if review_count else "Next Review",
        )
        self.next_review_issue.setEnabled(review_count > 0 and not APP_STATE.busy)

    def _next_ocr_issue(self) -> None:
        queue = WORKSPACE.ocr_review_queue()
        if not queue:
            self.status.setText("No suspicious OCR blocks found in completed pages")
            self._update_ocr_queue_status()
            return
        current_image = APP_STATE.selected_image
        current_block = APP_STATE.selected_block
        selected = None
        for item in queue:
            if (item["image_index"], item["block_index"]) > (current_image, current_block):
                selected = item
                break
        selected = selected or queue[0]
        APP_STATE.select(int(selected["image_index"]), int(selected["block_index"]))
        self.status.setText(
            f"OCR review page {selected['page']}, block {selected['group_index']}: "
            + ", ".join(selected["reasons"][:3])
        )

    def _next_review_issue(self) -> None:
        queue = WORKSPACE.review_issue_queue()
        if not queue:
            self.status.setText("No review issues found in completed pages")
            self._update_ocr_queue_status()
            return
        current_image = APP_STATE.selected_image
        current_block = APP_STATE.selected_block
        selected = None
        for item in queue:
            if (item["image_index"], item["block_index"]) > (current_image, current_block):
                selected = item
                break
        selected = selected or queue[0]
        APP_STATE.select(int(selected["image_index"]), int(selected["block_index"]))
        self.status.setText(
            f"Review page {selected['page']}, block {selected['group_index']}: "
            + ", ".join(selected["reasons"][:3])
        )

    def _on_workspace_image_updated(self, index: int) -> None:
        if not WORKSPACE.current or not (0 <= index < len(WORKSPACE.current.images)):
            return
        image = WORKSPACE.current.images[index]
        item = self._filmstrip_items.get(image.id)
        if item is not None:
            self._update_filmstrip_item(item, image, index)
        if index == APP_STATE.selected_image:
            self._load_image(index)

    def _on_selection(self, image: int, block: int) -> None:
        if (
            image >= 0
            and self._ignore_next_open_page_selection > 0
            and self.identity_tile.isChecked()
            and self.canvas_stack.currentWidget() is self.identity_preview
        ):
            self._ignore_next_open_page_selection -= 1
            APP_STATE.selected_image = -1
            APP_STATE.selected_block = -1
            self._clear_filmstrip_current()
            self._show_identity_workspace()
            return
        if image >= 0:
            self._ignore_next_open_page_selection = 0
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

    def _show_working(self, title: str, message: str) -> WorkingDialog:
        dialog = WorkingDialog(title, message, self)
        dialog.show()
        QApplication.processEvents()
        return dialog

    def _close_working(self, dialog: WorkingDialog | None) -> None:
        if dialog is None:
            return
        dialog.accept()
        QApplication.processEvents()

    def _apply(self) -> None:
        selected_rows = set(APP_STATE.selected_blocks)
        if not selected_rows and (0 <= APP_STATE.selected_block < len(self._groups)):
            selected_rows = {APP_STATE.selected_block}
        valid_rows = [r for r in selected_rows if 0 <= r < len(self._groups)]
        if not valid_rows:
            return

        image_index = APP_STATE.selected_image
        before_state = self._capture_editor_history_state(image_index)
        working = self._show_working("Apply & Rerender", f"Preparing {len(valid_rows)} selected bubble(s)...")
        try:
            working.set_message(f"Saving edits for {len(valid_rows)} bubble(s)...")
            is_single = (len(valid_rows) == 1)
            edits_map: dict[int | str, RegionEdit] = {}
            for row in valid_rows:
                group = self._groups[row]
                grp_index = group["index"]
                existing = WORKSPACE.current.images[image_index].edits.get(str(grp_index), RegionEdit()) if WORKSPACE.current else RegionEdit()
                pending_layout = self._pending_text_layouts.get(self._layout_key(image_index, grp_index))

                orig_text = self.original_text.toPlainText() if is_single else group.get("original_text", "")
                trans_text = self.translation.toPlainText() if is_single else group.get("translated_text", "")

                edit = RegionEdit(
                    translated_text=trans_text,
                    replace=self.replace.isChecked(),
                    font_size=0 if pending_layout else self.font_size.value(),
                    offset_x=self.offset_x.value(),
                    offset_y=self.offset_y.value(),
                    font_family=self.font.currentText(),
                    color=self.color.text(),
                    alignment=self.alignment.currentData(),
                    original_text=orig_text,
                    bubble_type=self.bubble_type.currentData(),
                    style_profile=self._style_profile_from_appearance(group, existing),
                    layout_x=pending_layout["x"] if pending_layout else existing.layout_x,
                    layout_y=pending_layout["y"] if pending_layout else existing.layout_y,
                    layout_width=pending_layout["width"] if pending_layout else existing.layout_width,
                    layout_height=pending_layout["height"] if pending_layout else existing.layout_height,
                    layout_angle=self.offset_angle.value() if pending_layout is None and self.offset_angle.value() != (existing.layout_angle or 0.0) else (pending_layout.get("angle", existing.layout_angle or 0.0) if pending_layout else existing.layout_angle),
                )
                WORKSPACE.validate_edit(image_index, grp_index, edit)
                edits_map[grp_index] = edit
                self._pending_text_layouts.pop(self._layout_key(image_index, grp_index), None)

            WORKSPACE.update_edits_batch(image_index, edits_map)

            working.set_message("Rendering the translated page...")
            WORKSPACE.rerender_image(image_index, log_callback=working.append_log)
            working.set_message("Refreshing the editor preview...")
            primary_row = valid_rows[0]
            self._load_image(image_index, primary_row)
            self._discard_text_layout_history(image_index, {str(index) for index in edits_map})
            self._push_editor_history("Apply & Rerender", image_index, before_state)
            if len(valid_rows) > 1:
                self._finish_bubble_selection_session()
                self.status.setText(f"Applied region settings to {len(valid_rows)} text bubbles and rerendered page")
            else:
                self.status.setText("Text style applied and page rerendered")
        except (MemoryError, OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._close_working(working); working = None
            QMessageBox.warning(self, "Could not render", render_error(error))
        finally:
            self._close_working(working)

    def _approve_ai_block(self) -> None:
        row = APP_STATE.selected_block
        if not (0 <= row < len(self._groups)):
            return
        group = self._groups[row]
        if not confirm(
            self,
            "Approve Bubble",
            "Approve the selected bubble and record its review outcome for learning?",
        ):
            return
        working = self._show_working("Approve Bubble", "Capturing review outcome...")
        try:
            working.set_message("Sending bubble approval to Hydra AI...")
            summary = WORKSPACE.approve_ai_block(APP_STATE.selected_image, group["index"])
            if summary is None:
                self._close_working(working); working = None
                QMessageBox.warning(self, "Hydra AI", hydra_ai_error(HYDRA_AI.error))
                return
            working.set_message("Refreshing review queues...")
            self._update_ocr_queue_status()
            self.status.setText(f"Approved {summary.approved} learning sample(s); skipped {summary.skipped}")
        finally:
            self._close_working(working)
        QMessageBox.information(
            self,
            "Bubble approved",
            f"Bubble approval completed.\n\nApproved: {summary.approved}\nSkipped: {summary.skipped}",
        )

    def _approve_ai_page_bubbles(self) -> None:
        if WORKSPACE.current is None or APP_STATE.selected_image < 0:
            return
        page_items = [item for item in WORKSPACE.ocr_review_queue() if int(item["image_index"]) == APP_STATE.selected_image]
        if not page_items:
            self.status.setText("No OCR/bubble issues remain on this page")
            self._update_ocr_queue_status()
            return
        if not confirm(
            self, "Approve page OCR",
            f"Approve all OCR/bubble review items on this page ({len(page_items)} block(s))?\n\n"
            "Only captured corrections become training samples; unchanged outputs remain review outcomes.",
        ):
            return
        working = self._show_working("Approve Page OCR", "Capturing OCR/bubble review outcomes...")
        try:
            working.set_message("Sending OCR/bubble approvals to Hydra AI...")
            summary = WORKSPACE.approve_ai_page_bubbles(APP_STATE.selected_image)
            if summary is None:
                self._close_working(working); working = None
                QMessageBox.warning(self, "Hydra AI", hydra_ai_error(HYDRA_AI.error))
                return
            working.set_message("Refreshing review queues...")
            self._update_ocr_queue_status()
            self.status.setText(f"Approved {summary.approved} learning sample(s); skipped {summary.skipped}")
        finally:
            self._close_working(working)
        QMessageBox.information(
            self,
            "Page OCR approved",
            f"Bubble review approval completed.\n\nApproved: {summary.approved}\nSkipped: {summary.skipped}",
        )

    def _approve_ai_page_reviews(self) -> None:
        if WORKSPACE.current is None or APP_STATE.selected_image < 0:
            return
        page_items = [item for item in WORKSPACE.review_issue_queue() if int(item["image_index"]) == APP_STATE.selected_image]
        if not page_items:
            self.status.setText("No review issues remain on this page")
            self._update_ocr_queue_status()
            return
        if not confirm(
            self, "Approve page review",
            f"Approve all non-OCR review items on this page ({len(page_items)} block(s))?\n\n"
            "Only captured corrections become training samples; unchanged outputs remain review outcomes.",
        ):
            return
        working = self._show_working("Approve Page Review", "Capturing page review outcomes...")
        try:
            working.set_message("Sending review approvals to Hydra AI...")
            summary = WORKSPACE.approve_ai_page_reviews(APP_STATE.selected_image)
            if summary is None:
                self._close_working(working); working = None
                QMessageBox.warning(self, "Hydra AI", hydra_ai_error(HYDRA_AI.error))
                return
            working.set_message("Refreshing review queues...")
            self._update_ocr_queue_status()
            self.status.setText(f"Approved {summary.approved} learning sample(s); skipped {summary.skipped}")
        finally:
            self._close_working(working)
        QMessageBox.information(
            self,
            "Page review approved",
            f"Review approval completed.\n\nApproved: {summary.approved}\nSkipped: {summary.skipped}",
        )

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

    def _begin_manual_box(self, mode: str | None = None, *, kind: str = "region") -> None:
        project = WORKSPACE.current
        index = APP_STATE.selected_image
        if project is None or not (0 <= index < len(project.images)):
            return
        mode = "polygon" if mode == "polygon" else "rectangle"
        kind = "title" if kind == "title" else "region"
        self._manual_creation_kind = kind
        self._region_cycle_mode = self._next_region_mode(mode)
        SETTINGS.manual_region_mode = mode
        try:
            SETTINGS.save()
        except OSError:
            pass
        if self.original.begin_manual_selection(mode):
            self._set_region_tool_active(mode, kind)
            message = (
                "Click around the title artwork, double-click to close"
                if kind == "title" and mode == "polygon" else
                "Draw a rectangle around the title artwork"
                if kind == "title" else
                "Click around one translatable text area, double-click to close"
                if mode == "polygon" else
                "Draw a rectangle around one translatable text area"
            )
            self.status.setText(message)
        else:
            self._reset_region_tool()

    def _manual_rect_created(self, rect: list[int]) -> None:
        self._reset_region_tool()
        image_index = APP_STATE.selected_image
        before_state = self._capture_editor_history_state(image_index)
        if WORKSPACE.request_manual_region(image_index, rect):
            if before_state is not None:
                self._pending_manual_history[image_index] = before_state
            return
        self._pending_manual_history.pop(image_index, None)
        self.status.setText("Manual translation is already running")

    def _manual_region_created(self, polygon: list[list[int]]) -> None:
        kind = self._manual_creation_kind
        self._reset_region_tool()
        self._manual_creation_kind = "region"
        image_index = APP_STATE.selected_image
        before_state = self._capture_editor_history_state(image_index)
        if kind == "title":
            if WORKSPACE.request_title_region(image_index, polygon):
                if before_state is not None:
                    self._pending_manual_history[image_index] = before_state
            else:
                self._pending_manual_history.pop(image_index, None)
                self.status.setText("Could not create title reconstruction region")
            return
        if WORKSPACE.request_manual_region(image_index, polygon):
            if before_state is not None:
                self._pending_manual_history[image_index] = before_state
            return
        self._pending_manual_history.pop(image_index, None)
        self.status.setText("Manual translation is already running")

    def _on_manual_region_busy(self, busy: bool) -> None:
        self._manual_busy = busy
        self.add_box.setEnabled(not busy)
        self.title_reconstruction.setEnabled(not busy)
        self.cancel_button.setEnabled(self._job_is_busy or busy)
        self.close_button.setEnabled(not (self._job_is_busy or busy))
        if busy:
            self.status.setText("Processing the selected text box...")
        else:
            self._reset_region_tool()

    def _on_translation_request_state(
        self,
        request_id: str,
        state: str,
        message: str,
    ) -> None:
        if request_id.startswith(("batch:", "selected:")):
            return

        # Save request configuration for potential retries
        request = WORKSPACE.manual_service._active.get(request_id)
        if request:
            self._recent_manual_requests[request_id] = request

        # Retrieve or create overlay
        overlay = self.original._manual_overlays.get(request_id)
        if not overlay:
            title = "Hydra"
            if request_id.startswith("title:") or (request and request.metadata.get("bubble_type") == "title"):
                operation = "Reconstructing Title..."
                stages = ["OCR", "Translation", "Reconstruction"]
            else:
                operation = "Translating Selection..."
                stages = ["OCR", "Translation", "Rendering"]

            polygon = None
            if request:
                polygon = request.metadata.get("polygon")
                if not polygon and request.manual_rect:
                    polygon = rect_to_polygon(request.manual_rect)
            if not polygon:
                polygon = [[0, 0], [100, 0], [100, 100], [0, 100]]

            overlay = self.original.add_manual_overlay(
                request_id, polygon, title=title, operation=operation, stages=stages
            )
            overlay.cancel_requested.connect(lambda rid: WORKSPACE.manual_service.cancel(rid))
            overlay.retry_requested.connect(self._retry_manual_request)

        labels = {
            "queued": "Manual translation queued",
            "ocr": "Reading selected text",
            "translating": "Translating selected text",
            "rendering": "Manual render queued",
            "done": "Manual text box translated",
            "cancelled": "Manual translation cancelled",
            "failed": "Manual translation failed",
        }
        status_msg = message or labels.get(state, state.title())
        self.status.setText(status_msg)

        if state == "queued":
            for stg in overlay.stage_names:
                overlay.update_stage(stg, "waiting")
        elif state == "ocr":
            overlay.update_stage(overlay.stage_names[0], "running")
        elif state == "translating":
            overlay.update_stage(overlay.stage_names[0], "completed")
            overlay.update_stage(overlay.stage_names[1], "running")
        elif state == "rendering":
            overlay.update_stage(overlay.stage_names[0], "completed")
            overlay.update_stage(overlay.stage_names[1], "completed")
            overlay.update_stage(overlay.stage_names[2], "running")
        elif state == "done":
            overlay.show_success("✓ Complete" if request_id.startswith("title:") else "✓ Translation Complete")
            QTimer.singleShot(800, lambda: overlay.start_fade_out())
        elif state == "cancelled":
            overlay.show_cancelled()
            QTimer.singleShot(800, lambda: overlay.start_fade_out())
        elif state == "failed":
            overlay.show_failure(status_msg)

    def _retry_manual_request(self, request_id: str) -> None:
        request = self._recent_manual_requests.get(request_id)
        if request:
            WORKSPACE.manual_service.submit(request)

    def _on_manual_region_finished(self, image_index: int, key: str) -> None:
        before_state = self._pending_manual_history.pop(image_index, None)
        if image_index != APP_STATE.selected_image:
            return
        self._load_image(image_index)
        row = next((index for index, group in enumerate(self._groups) if str(group.get("index")) == key), -1)
        if row >= 0:
            APP_STATE.select(image_index, row)
        group = self._groups[row] if row >= 0 else {}
        label = "Create Title Region" if group.get("bubble_type") == "title" else "Create Manual Region"
        self._push_editor_history(label, image_index, before_state)
        self.status.setText("Title reconstruction region created" if group.get("bubble_type") == "title" else "Manual text box translated")

    def _on_manual_region_failed(self, image_index: int, message: str) -> None:
        self._pending_manual_history.pop(image_index, None)
        if image_index == APP_STATE.selected_image:
            self.status.setText("Manual translation failed")
        # Do not show QMessageBox popup window to satisfy Option C "No popup windows"

    def _cancel_manual_draw(self) -> None:
        self.original.cancel_manual_selection()
        self._reset_region_tool()
        self.status.setText("Manual region drawing cancelled")
        # Also cancel active overlays
        for overlay in list(self.original._manual_overlays.values()):
            overlay._on_cancel_clicked()

    def _delete_selected_context(self) -> None:
        focus = QApplication.focusWidget()
        if focus is self.filmstrip or (focus is not None and self.filmstrip.isAncestorOf(focus)):
            self._delete_selected_images()
            return
        self._remove_selected_block()

    def _delete_selected_manual_block(self) -> None:
        """Backward-compatible entry point for the editor Delete action."""
        self._remove_selected_block()

    def _select_pending_images(self) -> None:
        if not WORKSPACE.current:
            return
        self.filmstrip.blockSignals(True)
        self.filmstrip.clearSelection()
        for item in self._filmstrip_items.values():
            image_id = str(item.data(Qt.ItemDataRole.UserRole))
            image = next((img for img in WORKSPACE.current.images if img.id == image_id), None)
            if image and image.status in TRANSLATE_ELIGIBLE_STATUSES:
                item.setSelected(True)
        self.filmstrip.blockSignals(False)
        self._selection_changed()
        self.status.setText("Selected pending images for translation")

    def _clear_all_selections(self) -> None:
        self.filmstrip.blockSignals(True)
        self.filmstrip.clearSelection()
        self.filmstrip.blockSignals(False)
        self._selection_changed()

        APP_STATE.select(APP_STATE.selected_image, -1)
        self.original.update_region_highlights(set())
        self.translated.update_region_highlights(set())
        self.blocks.blockSignals(True)
        self.blocks.clearSelection()
        self.blocks.setCurrentRow(-1)
        self.blocks.blockSignals(False)
        self.remove_block.setText("Remove Block")
        self.status.setText("Selection cleared")

    def _toggle_bubble_selector(self) -> None:
        if self.bubble_selector.isChecked():
            self.add_box.setChecked(False)
            self.title_reconstruction.setChecked(False)
            self.original.begin_bubble_selection()
            self.status.setText("Bubble Selector active: Drag box or Ctrl/Shift-click to toggle bubbles")
        else:
            self.original.cancel_manual_selection()
            self.status.setText("Bubble Selector deactivated")

    def _finish_bubble_selection_session(self) -> None:
        APP_STATE.selected_block = -1
        APP_STATE.selected_blocks.clear()
        if self.bubble_selector.isChecked():
            self.bubble_selector.setChecked(False)
        self.original.cancel_manual_selection()
        self.original.update_region_highlights(set())
        self.translated.update_region_highlights(set())
        self.blocks.blockSignals(True)
        self.blocks.clearSelection()
        self.blocks.setCurrentRow(-1)
        self.blocks.blockSignals(False)
        self.remove_block.setText("Remove Block")
        self.remove_block.setEnabled(False)
        self.apply_button.setText("Apply && Rerender")

    def _batch_blocks_selected(self, primary_row: int, selected_rows: set[int]) -> None:
        APP_STATE.select(APP_STATE.selected_image, primary_row, selected_rows)
        self.original.update_region_highlights(selected_rows)
        self.translated.update_region_highlights(selected_rows)
        self.blocks.blockSignals(True)
        self.blocks.clearSelection()
        for row in selected_rows:
            if 0 <= row < self.blocks.count():
                item = self.blocks.item(row)
                if item:
                    item.setSelected(True)
        if 0 <= primary_row < self.blocks.count():
            self.blocks.setCurrentRow(primary_row)
        self.blocks.blockSignals(False)
        count = len(selected_rows)
        if count > 1:
            self.status.setText(f"{count} text bubbles selected on page")
            self.remove_block.setText(f"Remove Blocks ({count})")
            self.remove_block.setEnabled(True)
            self.apply_button.setText(f"Apply && Rerender ({count})")
        else:
            self.remove_block.setText("Remove Block")
            self.apply_button.setText("Apply && Rerender")

    def _text_blocks_selection_changed(self) -> None:
        selected_items = self.blocks.selectedItems()
        selected_rows = {self.blocks.row(item) for item in selected_items if item is not None}
        current_row = self.blocks.currentRow()
        if selected_rows:
            primary = current_row if current_row in selected_rows else next(iter(selected_rows))
            APP_STATE.select(APP_STATE.selected_image, primary, selected_rows)
            self.original.update_region_highlights(selected_rows)
            self.translated.update_region_highlights(selected_rows)
            if 0 <= primary < len(self._groups):
                self._load_block(primary)
            count = len(selected_rows)
            if count > 1:
                self.remove_block.setText(f"Remove Blocks ({count})")
                self.remove_block.setEnabled(True)
                self.apply_button.setText(f"Apply && Rerender ({count})")
            else:
                self.remove_block.setText("Remove Block")
                self.apply_button.setText("Apply && Rerender")
        else:
            APP_STATE.select(APP_STATE.selected_image, -1, set())
            self.original.update_region_highlights(set())
            self.translated.update_region_highlights(set())
            self.remove_block.setText("Remove Block")
            self.remove_block.setEnabled(False)
            self.apply_button.setText("Apply && Rerender")

    def _remove_selected_block(self) -> None:
        selected_rows = set(APP_STATE.selected_blocks)
        if not selected_rows and (0 <= APP_STATE.selected_block < len(self._groups)):
            selected_rows = {APP_STATE.selected_block}
        valid_rows = [r for r in selected_rows if 0 <= r < len(self._groups)]
        if not valid_rows:
            return
        if len(valid_rows) > 1:
            answer = QMessageBox.question(
                self,
                "Delete Multiple Bubbles",
                f"Delete the {len(valid_rows)} selected text bubbles?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            image_index = APP_STATE.selected_image
            before_state = self._capture_editor_history_state(image_index)
            working = self._show_working("Remove Selected Text Blocks", "Removing selected bubbles...")
            try:
                for row in sorted(valid_rows, reverse=True):
                    if 0 <= row < len(self._groups):
                        grp = self._groups[row]
                        if bool(grp.get("manual")):
                            WORKSPACE.delete_manual_region(image_index, str(grp["index"]))
                        else:
                            WORKSPACE.suppress_auto_region(image_index, int(grp["index"]))
                self._load_image(image_index, -1)
                self._push_editor_history("Delete Text Blocks", image_index, before_state)
                self.status.setText(f"{len(valid_rows)} text bubbles removed")
            finally:
                self._close_working(working)
            return

        row = valid_rows[0]
        group = self._groups[row]
        is_manual = bool(group.get("manual"))
        label = "manual bubble" if is_manual else "automatic bubble"
        answer = QMessageBox.question(
            self,
            "Delete Bubble",
            f"Delete the selected {label}?\n\n"
            + (
                "Covered automatic bubbles will be restored."
                if is_manual
                else "You can restore this bubble later with Restore Auto."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        success_message = ""
        image_index = APP_STATE.selected_image
        before_state = self._capture_editor_history_state(image_index)
        working = self._show_working("Remove Text Block", "Updating the translated page...")
        try:
            if is_manual:
                working.set_message("Deleting the manual block and restoring covered regions...")
                removed = WORKSPACE.delete_manual_region(image_index, str(group["index"]))
                message = "Manual text box deleted; its automatic blocks were restored"
            else:
                working.set_message("Removing the automatic block...")
                removed = WORKSPACE.suppress_auto_region(image_index, int(group["index"]))
                message = "Automatic block removed; draw an Add Text Box replacement if needed"
            if removed:
                working.set_message("Refreshing the editor preview...")
                self._load_image(image_index, max(-1, row - 1))
                self._push_editor_history("Delete Text Block", image_index, before_state)
                self.status.setText(message)
                success_message = message
        except (MemoryError, OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._close_working(working); working = None
            QMessageBox.warning(self, "Could not remove text block", render_error(error))
        finally:
            self._close_working(working)
        if success_message:
            QMessageBox.information(self, "Bubble deleted", success_message)

    def _restore_auto_blocks(self) -> None:
        image_index = APP_STATE.selected_image
        before_state = self._capture_editor_history_state(image_index)
        try:
            if WORKSPACE.restore_auto_regions(image_index):
                self._load_image(image_index)
                self._push_editor_history("Restore Auto Blocks", image_index, before_state)
                self.status.setText("Removed automatic blocks restored")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            QMessageBox.warning(self, "Could not restore automatic blocks", render_error(error))

    def _choose_color(self) -> None:
        color = QColorDialog.getColor(QColor(self.color.text()), self)
        if color.isValid(): self._update_color_swatch(color.name())

    def _choose_gradient_start(self) -> None:
        color = QColorDialog.getColor(QColor(self.gradient_start.text()), self)
        if color.isValid():
            self._update_gradient_start_swatch(color.name())

    def _choose_gradient_end(self) -> None:
        color = QColorDialog.getColor(QColor(self.gradient_end.text()), self)
        if color.isValid():
            self._update_gradient_end_swatch(color.name())

    def _set_color_swatch(self, button: QPushButton, value: str, tooltip: str) -> None:
        color = QColor(value)
        if not color.isValid(): color = QColor("#111111")
        pixmap = QPixmap(18, 18); pixmap.fill(color)
        button.setIcon(QIcon(pixmap)); button.setText(color.name())
        button.setToolTip(f"{tooltip} ({color.name()})")

    def _update_color_swatch(self, value: str) -> None:
        self._set_color_swatch(self.color, value, "Choose text color")

    def _update_gradient_start_swatch(self, value: str) -> None:
        self._set_color_swatch(self.gradient_start, value, "Choose gradient start color")

    def _update_gradient_end_swatch(self, value: str) -> None:
        self._set_color_swatch(self.gradient_end, value, "Choose gradient end color")

    def _update_gradient_controls_enabled(self, checked: bool = False) -> None:
        art_region = self._current_region_uses_art_appearance()
        self.gradient_enabled.setEnabled(art_region)
        self.gradient_start.setEnabled(art_region and checked)
        self.gradient_end.setEnabled(art_region and checked)
        self.gradient_angle.setEnabled(art_region and checked)
        if art_region:
            self.gradient_enabled.setToolTip("Use two colors for title, SFX, sign, or credit text")
        else:
            self.gradient_enabled.setToolTip("Gradient fill is available for title, SFX, sign, and credit regions")

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
        if SettingsDialog(self).exec() == QDialog.DialogCode.Accepted:
            self._configure_manual_shortcut()
            self._region_cycle_mode = SETTINGS.manual_region_mode or "rectangle"
            self._filmstrip_policy_mode = SETTINGS.filmstrip_collapse_mode or "current"
            if WORKSPACE.current is not None:
                project_id = str(getattr(WORKSPACE.current, "id", WORKSPACE.current.name))
                self._apply_filmstrip_collapse_preference(WORKSPACE.current, project_id)
            self.status.setText("Translation provider settings saved")

    def _update_project_title(self) -> None:
        display_title = self._compact_project_title(self._project_title_full)
        self.project_title.setText(display_title)
        self.project_title.setToolTip(self._project_title_full)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_project_title()
        self._update_header_responsive_mode()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._update_header_responsive_mode()

    def _move_image(self, delta: int) -> None:
        if WORKSPACE.current and WORKSPACE.current.images: APP_STATE.select(max(0, min(len(WORKSPACE.current.images)-1, APP_STATE.selected_image + delta)))

    def _fit_both(self) -> None:
        if hasattr(self, "canvas_stack") and self.canvas_stack.currentWidget() is self.identity_preview:
            self.identity_preview.fit_image()
            return
        self.original.fit_image(); self.translated.fit_image()

    def _actual_both(self) -> None:
        if hasattr(self, "canvas_stack") and self.canvas_stack.currentWidget() is self.identity_preview:
            self.identity_preview.actual_size()
            return
        self.original.actual_size(); self.translated.actual_size()

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
            self._notify_translation_finished()
        elif stage == "cancelled" and not self._job_is_busy:
            self._schedule_job_panel_collapse("cancelled")

    def _notify_translation_finished(self) -> None:
        """Fire desktop notifications at translation pipeline terminal state.

        Called only from the ``stage == 'ready'`` branch of ``_on_pipeline``.
        Reads failure/total counts from the already-updated progress state so
        no extra state needs to be maintained.
        """
        total = self._job_total
        failures = self._job_failure_count
        if total == 0:
            return
        from hydra_manga_tl.core.notifications import NOTIFICATION_SERVICE, NotificationEvent
        from hydra_manga_tl.project.workspace import WORKSPACE
        if failures:
            NOTIFICATION_SERVICE.notify(
                NotificationEvent.TRANSLATION_FAILED,
                "Translation finished with errors",
                f"{total - failures} page(s) done, {failures} failed.",
            )
        else:
            NOTIFICATION_SERVICE.notify(
                NotificationEvent.TRANSLATION_COMPLETED,
                "Translation complete",
                f"{total} page(s) translated successfully.",
            )
        # Review queue is a separate, additive notification
        project = WORKSPACE.current
        if project:
            review_count = sum(1 for img in project.images if img.status == "review")
            if review_count:
                NOTIFICATION_SERVICE.notify(
                    NotificationEvent.REVIEW_QUEUE,
                    "Review queue",
                    f"{review_count} page(s) need review.",
                )

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
            "preprocessing": "Preparing", "analyzing": "Preparing", "OCR": "Reading", "ocr": "Reading",
            "translating": "Translating", "rendering": "Rebuilding", "reconstructing": "Rebuilding",
            "review": "Reviewing", "complete": "Complete", "failed": "Failed",
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
        aliases = {"analyzing": "preprocessing", "OCR": "ocr", "rendering": "reconstructing", "ready": "complete", "done": "complete"}
        active = aliases.get(active, active)
        stages = [("preprocessing", "Preparing"), ("ocr", "Reading"), ("translating", "Translating"), ("reconstructing", "Rendering"), ("review", "Review"), ("complete", "Complete")]
        active_index = next((index for index, (key, _) in enumerate(stages) if key == active), -1)
        if active in {"complete"}:
            active_index = len(stages)
        values = []
        for index, (_, label) in enumerate(stages):
            marker = "[x]" if index < active_index or active == "complete" else ("[>]" if index == active_index else "[ ]")
            values.append(f"{marker} {label}")
        suffix = "     [!] Failed" if active == "failed" else ("     [!] Cancelled" if active == "cancelled" else "")
        return "     ".join(values) + suffix

    def _on_busy(self, busy: bool) -> None:
        self._job_is_busy = busy
        self.filmstrip.set_reorder_enabled(not busy)
        pending = bool(WORKSPACE.current and any(image.status in TRANSLATE_ELIGIBLE_STATUSES for image in WORKSPACE.current.images))
        self.start_button.setEnabled(not busy and pending)
        self.cancel_button.setEnabled(busy or self._manual_busy)
        self.close_button.setEnabled(not (busy or self._manual_busy))
        self.add_box.setEnabled(not self._manual_busy)
        self.title_reconstruction.setEnabled(not self._manual_busy)
        self._selection_changed()
        keep_result = self._job_total > 0 and self._progress_stage in {"ready", "cancelled", "failed", "complete"}
        self.progress.setVisible(busy or keep_result); self.page_progress.setVisible(busy or keep_result)
        if not busy and not keep_result:
            self.progress.setValue(0); self.page_progress.setValue(0)

    @staticmethod
    def _matches_review_filter(region: dict, category: str) -> bool:
        if category == "untranslated":
            return region.get("original_text") == region.get("translated_text")
        if category == "residual_source":
            text = region.get("translated_text") or ""
            return any(0x3000 <= ord(c) <= 0x9FFF or 0xFF00 <= ord(c) <= 0xFFEF for c in text)
        if category == "overflow":
            return "text_does_not_fit" in region.get("review_reasons", [])
        if category == "missing_glyph":
            return "□" in region.get("translated_text", "")
        if category == "low_ocr":
            return region.get("ocr_confidence", 1.0) < 0.6
        if category == "provider_fallback":
            return region.get("translation_source") == "fallback"
        return False

    def _default_export_target(self) -> tuple[Path, str]:
        import re
        parent = Path(SETTINGS.export_root)
        name = "manga"
        if WORKSPACE.current:
            raw_name = WORKSPACE.current.name or "manga"
            name = re.sub(r"[^a-zA-Z0-9_\-]+", "_", raw_name).strip("_")
        return parent, name

    def _start_export_worker(self, output_type: str, destination: Path, *, image_format: str = "png", archive_format: str = "zip") -> None:
        self._export_dialog = BackgroundWorkDialog(self)
        self._export_dialog.setWindowTitle("Exporting")
        self._export_dialog.message.setText(f"Exporting files to:\n{destination}\n\nPlease wait...")
        self._export_dialog.set_progress_visible(True)
        self._export_dialog.set_progress_fraction(0, 1)

        self._export_thread = QThread(self)
        worker = ExportWorker(output_type, destination, image_format=image_format, archive_format=archive_format)
        worker.moveToThread(self._export_thread)

        self._export_thread.started.connect(worker.run)

        worker.progress.connect(self._on_export_progress)
        worker.finished.connect(self._on_export_finished)
        worker.failed.connect(self._on_export_failed)

        worker.finished.connect(self._export_thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(self._export_thread.quit)
        worker.failed.connect(worker.deleteLater)
        self._export_thread.finished.connect(self._export_thread.deleteLater)

        self._export_thread.start()
        self._export_dialog.exec()
        self._export_dialog = None
        self._export_thread = None

    @Slot(int, int)
    def _on_export_progress(self, current: int, total: int) -> None:
        if hasattr(self, "_export_dialog") and self._export_dialog is not None:
            self._export_dialog.set_progress_fraction(current, total)

    @Slot(str, object)
    def _on_export_finished(self, ot: str, result):
        if hasattr(self, "_export_dialog") and self._export_dialog is not None:
            self._export_dialog.accept()
        if ot == "folder":
            QMessageBox.information(self, "Export complete", f"Exported {result} image(s).")
        elif ot == "pdf":
            QMessageBox.information(self, "Export complete", f"Exported PDF:\n{result}")
        else:
            QMessageBox.information(self, "Export complete", f"Exported archive:\n{result}")
        from hydra_manga_tl.core.notifications import NOTIFICATION_SERVICE, NotificationEvent
        from pathlib import Path as _Path
        if ot == "folder":
            notif_msg = f"Exported {result} image(s)."
        else:
            notif_msg = f"Saved: {_Path(str(result)).name}"
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

    def _export(self) -> None:
        dialog = ExportOptionsDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        output_type = str(dialog.output_type.currentData())
        image_format = str(dialog.image_format.currentData())

        export_root, base_name = self._default_export_target()

        if output_type == "folder":
            folder = QFileDialog.getExistingDirectory(self, "Export image folder", str(export_root))
            if not folder:
                return
            self._start_export_worker(output_type, Path(folder) / base_name, image_format=image_format)
            return

        if output_type == "pdf":
            default_pdf_path = str(export_root / f"{base_name}.pdf")
            path, _ = QFileDialog.getSaveFileName(self, "Export PDF", default_pdf_path, "PDF document (*.pdf);;All files (*.*)")
            if not path:
                return
            self._start_export_worker(output_type, Path(path))
            return

        archive_format = "cbz" if output_type == "cbz" else "zip"
        filter_label = "CBZ comic archive (*.cbz)" if archive_format == "cbz" else "ZIP archive (*.zip)"
        default_archive_path = str(export_root / f"{base_name}.{archive_format}")
        path, _ = QFileDialog.getSaveFileName(self, "Export archive", default_archive_path, f"{filter_label};;All files (*.*)")
        if not path:
            return
        self._start_export_worker(output_type, Path(path), image_format=image_format, archive_format=archive_format)
