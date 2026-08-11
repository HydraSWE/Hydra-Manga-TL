"""Main translation workspace screen."""


from __future__ import annotations

import json
import sys
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
from hydra_manga_tl.core.user_errors import hydra_ai_error, pipeline_error, render_error, workspace_action_error
from hydra_manga_tl.ui.canvas import CanvasView
from hydra_manga_tl.ui.dialogs import AiCenterDialog, BackgroundWorkDialog, ExportOptionsDialog, GlossaryDialog, IdentityPreviewDialog, PhraseMemoryManagerDialog, SettingsDialog, WorkingDialog
from hydra_manga_tl.ui.filmstrip import ReorderableFilmstrip
from hydra_manga_tl.ui.shared import CollapsibleSection, FILMSTRIP_CARD_SIZE, FILMSTRIP_PREVIEW_SIZE, TARGET_LANGUAGE_NAMES, _language_badge, _page_label, _speaker_icon, lucide_icon, confirm
from .constants import PROGRESS_RANGES, TRANSLATE_ELIGIBLE_STATUSES
from .export_worker import ExportWorker
from .export_controller import ExportControllerMixin
from .editor_controller import EditorControllerMixin
from .filmstrip_controller import FilmstripControllerMixin
from .inspector_controller import InspectorControllerMixin
from .manual_region_controller import ManualRegionControllerMixin
from .progress_controller import ProgressControllerMixin
from .responsive_header import ResponsiveHeaderMixin
from hydra_manga_tl.project.workspace import WORKSPACE
from hydra_manga_tl.core.ai_bridge import HYDRA_AI


class WorkspaceScreen(
    InspectorControllerMixin,
    EditorControllerMixin,
    ManualRegionControllerMixin,
    ProgressControllerMixin,
    FilmstripControllerMixin,
    ResponsiveHeaderMixin,
    ExportControllerMixin,
    QWidget,
):
    close_requested = Signal()
    _ART_APPEARANCE_TYPES = {"title", "sfx", "sign", "credit"}
    _HEADER_COMPACT_ENTER_WIDTH = 1340
    _HEADER_COMPACT_EXIT_WIDTH = 1440

    _PROGRESS_RANGES = PROGRESS_RANGES

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
        self._filmstrip_build_chunk_size = 24
        self._filmstrip_build_generation = 0
        self._pending_image_load: tuple[int, int] | None = None
        self._image_load_pending = False
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
        self.inspector.setMinimumWidth(540); self.inspector.setMaximumWidth(760); main.setSizes([1200, 640])
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

