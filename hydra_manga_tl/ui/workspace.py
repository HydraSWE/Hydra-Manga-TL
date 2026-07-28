"""Main translation workspace screen."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QModelIndex, QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame, QGraphicsView, QGridLayout, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter, QStackedWidget, QStyle, QTabWidget, QSizePolicy, QTextEdit, QToolButton, QVBoxLayout, QWidget, QKeySequenceEdit, QLineEdit

from hydra_manga_tl.core.assets import find_asset
from hydra_manga_tl.project.editor import RegionEdit
from hydra_manga_tl.project.import_scan import ThumbnailWorker
from hydra_manga_tl.core.language import resolve_source_language
from hydra_manga_tl.project.manual_region import normalize_image_rect, rect_to_polygon
from hydra_manga_tl.core.region_types import normalize_region_type
from hydra_manga_tl.core.settings import SETTINGS
from hydra_manga_tl.core.speech import SpeechService
from hydra_manga_tl.core.state import APP_STATE
from hydra_manga_tl.ui.canvas import CanvasView
from hydra_manga_tl.ui.dialogs import AiCenterDialog, ExportOptionsDialog, GlossaryDialog, IdentityPreviewDialog, SettingsDialog, WorkingDialog
from hydra_manga_tl.ui.filmstrip import ReorderableFilmstrip
from hydra_manga_tl.ui.shared import CollapsibleSection, FILMSTRIP_CARD_SIZE, FILMSTRIP_PREVIEW_SIZE, TARGET_LANGUAGE_NAMES, _language_badge, _page_label, _speaker_icon
from hydra_manga_tl.project.workspace import WORKSPACE
from hydra_manga_tl.core.ai_bridge import HYDRA_AI


class WorkspaceScreen(QWidget):
    close_requested = Signal()

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
        self._pending_text_layouts: dict[tuple[int, str], dict] = {}
        self._ignore_next_open_page_selection = False
        self._recent_manual_requests: dict[str, object] = {}
        self.speech = SpeechService(self)
        self.speech.unavailable.connect(lambda message: QMessageBox.information(self, "Original text voice", message))
        self._build()
        self._configure_manual_shortcut()
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
        WORKSPACE.translation_request_state_changed.connect(
            self._on_translation_request_state,
        )

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
        self.cancel_button = QPushButton("Cancel"); self.cancel_button.clicked.connect(WORKSPACE.cancel_active_requests); self.cancel_button.setEnabled(False)
        save = QPushButton("Save"); save.clicked.connect(WORKSPACE.save)
        export = QPushButton("Export"); export.clicked.connect(self._export)
        self.close_button = QPushButton("Close"); self.close_button.clicked.connect(self.close_requested)
        settings = QPushButton("Settings"); settings.clicked.connect(self._open_settings)
        ai_center = QPushButton("AI Center"); ai_center.clicked.connect(lambda: AiCenterDialog(self).exec())
        glossary = QPushButton("Glossary"); glossary.clicked.connect(lambda: GlossaryDialog(self).exec())
        for widget in (self.project_title, self.count_label): row.addWidget(widget)
        row.addStretch()
        for widget in (self.source_combo, self.target_combo, self.quality_combo, self.style_combo, self.selected_button, self.start_button, self.cancel_button, glossary, ai_center, settings, save, export, self.close_button): row.addWidget(widget)
        root.addWidget(header)

        tools = QHBoxLayout()
        previous = QPushButton("‹"); previous.clicked.connect(lambda: self._move_image(-1))
        next_button = QPushButton("›"); next_button.clicked.connect(lambda: self._move_image(1))
        fit = QPushButton("Fit"); fit.clicked.connect(self._fit_both)
        actual = QPushButton("100%"); actual.clicked.connect(self._actual_both)
        self.next_ocr_issue = QPushButton("Next OCR Issue"); self.next_ocr_issue.clicked.connect(self._next_ocr_issue)
        self.next_review_issue = QPushButton("Next Review Issue"); self.next_review_issue.clicked.connect(self._next_review_issue)
        self.add_box = QToolButton()
        self.add_box.setObjectName("ToolbarButton")
        self.add_box.setText("Region Tool")
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
        self.title_reconstruction.setText("Title Reconstruction")
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
        self.image_label = QLabel("No image")
        self.selection_label = QLabel("1 selected"); self.selection_label.setObjectName("Muted")
        tools.addWidget(previous); tools.addWidget(next_button); tools.addWidget(self.image_label); tools.addWidget(self.selection_label); tools.addStretch(); tools.addWidget(self.next_ocr_issue); tools.addWidget(self.next_review_issue); tools.addWidget(self.add_box); tools.addWidget(self.title_reconstruction); tools.addWidget(fit); tools.addWidget(actual)
        root.addLayout(tools)

        main = QSplitter(Qt.Orientation.Horizontal); self.main_splitter = main
        main.setChildrenCollapsible(False)
        canvas_host = QWidget(); canvas_layout = QVBoxLayout(canvas_host); canvas_layout.setContentsMargins(0, 0, 0, 0)
        self.canvas_stack = QStackedWidget()
        canvases = QSplitter(Qt.Orientation.Horizontal)
        self.original = CanvasView("Original"); self.translated = CanvasView("Translated")
        canvases.addWidget(self.original); canvases.addWidget(self.translated); canvases.setSizes([600, 600])
        self.page_canvases = canvases
        self.identity_preview = CanvasView("Hydra Identity")
        self.identity_preview.setObjectName("IdentityWorkspacePreview")
        self.canvas_stack.addWidget(self.page_canvases)
        self.canvas_stack.addWidget(self.identity_preview)
        canvas_layout.addWidget(self.canvas_stack)
        self.filmstrip_section = CollapsibleSection("Filmstrip", expanded=True)
        self.filmstrip_section.setObjectName("FilmstripSection")
        self.filmstrip_section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.filmstrip_section.body.setMaximumHeight(132)
        self.filmstrip_section.expanded_changed.connect(self._filmstrip_expanded_changed)
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
        self.translated.text_layout_changed.connect(self._text_layout_changed)
        self.original.manual_region_created.connect(self._manual_region_created)
        self.original.manual_region_message.connect(self.status.setText)
        self.original.manual_selection_finished.connect(self._reset_region_tool)
        self.original.zoom_changed.connect(self.translated.set_zoom); self.translated.zoom_changed.connect(self.original.set_zoom)
        self._sync_scrollbars(self.original, self.translated); self._sync_scrollbars(self.translated, self.original)
        self._layout_undo_shortcut = QShortcut(QKeySequence.StandardKey.Undo, self)
        self._layout_undo_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._layout_undo_shortcut.activated.connect(self._undo_text_layout)
        self._layout_redo_shortcut = QShortcut(QKeySequence.StandardKey.Redo, self)
        self._layout_redo_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._layout_redo_shortcut.activated.connect(self._redo_text_layout)
        self._configure_editor_shortcuts()
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._application_focus_changed)

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

    def _refresh_region_tool_style(self) -> None:
        for button in (self.add_box, getattr(self, "title_reconstruction", None)):
            if button is None:
                continue
            button.style().unpolish(button)
            button.style().polish(button)

    def _set_region_tool_active(self, mode: str, kind: str = "region") -> None:
        label = "Polygon" if mode == "polygon" else "Rectangle"
        if kind == "title":
            self.title_reconstruction.setText(f"Title {label}")
            self.title_reconstruction.setChecked(True)
            self.add_box.setText("Region Tool")
            self.add_box.setChecked(False)
        else:
            self.add_box.setText(label)
            self.add_box.setChecked(True)
            self.title_reconstruction.setText("Title Reconstruction")
            self.title_reconstruction.setChecked(False)
        self._refresh_region_tool_style()

    def _reset_region_tool(self) -> None:
        self.add_box.setText("Region Tool")
        self.add_box.setChecked(False)
        self.title_reconstruction.setText("Title Reconstruction")
        self.title_reconstruction.setChecked(False)
        self._refresh_region_tool_style()

    def _build_inspector(self) -> QFrame:
        frame = QFrame(); frame.setObjectName("Inspector"); layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8); layout.setSpacing(6)
        tabs = QTabWidget(); layout.addWidget(tabs)
        text_tab = QWidget(); text_layout = QVBoxLayout(text_tab)
        text_layout.setContentsMargins(8, 7, 8, 8); text_layout.setSpacing(7)
        self.blocks = QListWidget(); self.blocks.setObjectName("TextBlocksList"); self.blocks.setWordWrap(True); self.blocks.setTextElideMode(Qt.TextElideMode.ElideRight); self.blocks.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); self.blocks.setMinimumHeight(140); self.blocks.setMaximumHeight(175); self.blocks.currentRowChanged.connect(self._select_block)
        text_layout.addWidget(self.blocks)
        editor_host = QWidget(); editor_layout = QVBoxLayout(editor_host)
        editor_layout.setContentsMargins(0, 0, 0, 0); editor_layout.setSpacing(7)
        form_host = QWidget(); form = QFormLayout(form_host); form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setContentsMargins(0, 0, 0, 0); form.setHorizontalSpacing(8); form.setVerticalSpacing(7)
        self.original_text = QTextEdit(); self.original_text.setReadOnly(False); self.original_text.setFixedHeight(50)
        self.original_text.setToolTip("Correct OCR source text here; approval is separate from Apply & Rerender")
        self.original_text.setMinimumWidth(0); self.original_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        original_host = QWidget(); original_host.setMinimumWidth(0); original_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        original_row = QHBoxLayout(original_host); original_row.setContentsMargins(0, 0, 0, 0); original_row.setSpacing(6)
        self.speak_original = QToolButton(); self.speak_original.setObjectName("SpeechButton"); self.speak_original.setIcon(_speaker_icon())
        self.speak_original.setIconSize(QSize(18, 18))
        self.speak_original.setToolTip("Play or stop the original text")
        self.speak_original.setFixedSize(30, 30)
        self.speak_original.setEnabled(False); self.speak_original.clicked.connect(self._speak_original)
        original_row.addWidget(self.original_text, 1); original_row.addWidget(self.speak_original)
        self.translation = QTextEdit(); self.translation.setFixedHeight(50)
        self.translation.setMinimumWidth(0); self.translation.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.confidence = QLabel("—"); self.confidence.setObjectName("Muted")
        self.confidence_bar = QProgressBar(); self.confidence_bar.setRange(0, 1000); self.confidence_bar.setTextVisible(False); self.confidence_bar.setFixedHeight(8)
        confidence_host = QWidget(); confidence_row = QHBoxLayout(confidence_host); confidence_row.setContentsMargins(0, 0, 0, 0); confidence_row.setSpacing(8)
        confidence_row.addWidget(self.confidence, 0); confidence_row.addWidget(self.confidence_bar, 1)
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
        self.color = QPushButton("#111111"); self.color.clicked.connect(self._choose_color)
        self.offset_x = QSpinBox(); self.offset_x.setRange(-500, 500); self.offset_x.setSuffix(" px")
        self.offset_y = QSpinBox(); self.offset_y.setRange(-500, 500); self.offset_y.setSuffix(" px")
        self.offset_x.setSingleStep(5); self.offset_y.setSingleStep(5)
        self.offset_x.setButtonSymbols(QSpinBox.ButtonSymbols.UpDownArrows)
        self.offset_y.setButtonSymbols(QSpinBox.ButtonSymbols.UpDownArrows)
        for label, widget in (("Original", original_host), ("Translation", self.translation), ("Confidence", confidence_host)): form.addRow(label, widget)
        form.addRow(self.replace)
        editor_layout.addWidget(self._build_static_inspector_section("1. Translation", form_host))
        editor_layout.addWidget(self._build_inspector_section("2. Region", (("Region type", self.bubble_type), ("Alignment", self.alignment)), expanded=True))
        editor_layout.addWidget(self._build_inspector_section("3. Typography", (("Font", self.font), ("Size", self.font_size)), expanded=False))
        editor_layout.addWidget(self._build_inspector_section("4. Transform", (("X", self.offset_x), ("Y", self.offset_y)), expanded=False))
        editor_layout.addWidget(self._build_inspector_section("5. Appearance", (("Color", self.color),), expanded=False))
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
        info_tab = QWidget(); info_layout = QFormLayout(info_tab)
        self.info_path = QLabel("—"); self.info_path.setWordWrap(True); self.info_language = QLabel("—"); self.info_status = QLabel("—")
        info_layout.addRow("Source", self.info_path); info_layout.addRow("Language", self.info_language); info_layout.addRow("Status", self.info_status); tabs.addTab(info_tab, "Image Info")
        return frame

    def _build_static_inspector_section(self, title: str, content: QWidget) -> QFrame:
        section = QFrame(self)
        section.setObjectName("InspectorSection")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(10, 7, 10, 8)
        layout.setSpacing(6)
        heading = QLabel(title)
        heading.setObjectName("InspectorSectionTitle")
        layout.addWidget(heading)
        layout.addWidget(content)
        return section

    def _build_inspector_section(self, title: str, rows: tuple[tuple[str, QWidget], ...], *, expanded: bool = False) -> CollapsibleSection:
        section = CollapsibleSection(title, expanded, self)
        form = QFormLayout(section.body)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setContentsMargins(10, 2, 10, 9)
        form.setHorizontalSpacing(8); form.setVerticalSpacing(7)
        for label, widget in rows:
            form.addRow(label, widget)
        return section

    def _sync_bubble_type_options(self, current_type: str) -> None:
        current_type = "title" if current_type == "title" else normalize_region_type(current_type or "dialogue")
        self.bubble_type.blockSignals(True)
        self.bubble_type.clear()
        options = [("Dialogue", "dialogue"), ("SFX", "sfx"), ("Sign", "sign"), ("Credit", "credit")]
        if current_type == "title":
            options.insert(1, ("Title", "title"))
        for label, value in options:
            self.bubble_type.addItem(label, value)
        index = self.bubble_type.findData(current_type)
        self.bubble_type.setCurrentIndex(max(0, index))
        self.bubble_type.blockSignals(False)

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
        return "always_collapsed" if mode == "always_collapsed" else "current"

    def _apply_filmstrip_collapse_preference(self, project, project_id: str) -> None:
        mode = self._filmstrip_collapse_mode()
        project_changed = project_id != self._filmstrip_policy_project_id
        mode_changed = mode != self._filmstrip_policy_mode
        if mode == "always_collapsed":
            if project_changed or mode_changed:
                self.filmstrip_section.set_expanded(False)
        else:
            self.filmstrip_section.set_expanded(bool(getattr(project, "filmstrip_visible", True)))
        self._filmstrip_policy_project_id = project_id
        self._filmstrip_policy_mode = mode

    def _show_identity_preview(self) -> None:
        if self.identity_thumbnail_path is None:
            return
        IdentityPreviewDialog(self.identity_thumbnail_path, self).exec()
        if self.filmstrip.currentRow() >= 0:
            self.filmstrip.setFocus()

    def refresh(self, project, *, force_filmstrip_rebuild: bool = False) -> None:
        if project is None:
            self._reset_project_view_state()
            return
        self._project_title_full = project.name; self.project_title.setToolTip(project.name); self._update_project_title()
        self.count_label.setText(f"{len(project.images)} images")
        page_count = len(project.images)
        self.filmstrip_section.toggle.setText(
            f"Filmstrip • {page_count} page{'s' if page_count != 1 else ''}"
        )
        self.start_button.setEnabled(not APP_STATE.busy and any(image.status in {"pending", "queued", "partial", "failed", "cancelled"} for image in project.images))
        self.quality_combo.setCurrentText(project.quality)
        self.style_combo.setCurrentText(project.text_style)
        source_index = self.source_combo.findData(project.source_language); self.source_combo.setCurrentIndex(max(0, source_index))
        current = (
            max(0, min(APP_STATE.selected_image, len(project.images) - 1))
            if project.images and APP_STATE.selected_image >= 0 else -1
        )
        image_ids = [self._image_id(image) for image in project.images]
        project_id = str(getattr(project, "id", project.name))
        self._apply_filmstrip_collapse_preference(project, project_id)
        live_items = self._current_filmstrip_items()
        project_changed = project_id != self._filmstrip_project_id
        show_identity_on_open = project_changed and self.identity_thumbnail_path is not None
        filmstrip_current = -1 if show_identity_on_open else current
        if force_filmstrip_rebuild or project_changed or image_ids != list(live_items):
            self._rebuild_filmstrip(project, project_id, image_ids, filmstrip_current)
            if show_identity_on_open:
                self._select_identity()
        else:
            self._filmstrip_items = live_items
            for image_index, image in enumerate(project.images):
                self._update_filmstrip_item(self._filmstrip_items[image_ids[image_index]], image, image_index)
            if current >= 0 and self.filmstrip.currentRow() < 0:
                self.filmstrip.setCurrentRow(current)
            elif current < 0:
                self._clear_filmstrip_current()
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
        self._groups = []
        if hasattr(self, "canvas_stack"):
            self.canvas_stack.setCurrentWidget(self.page_canvases)

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
        self._ignore_next_open_page_selection = True
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
        self._filmstrip_project_id = project_id
        if current >= 0:
            self.filmstrip.setCurrentRow(current)
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
            if self.filmstrip.item(row) is not None
        }

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
            self._ignore_next_open_page_selection = False
            self.identity_tile.setChecked(False)
            APP_STATE.select(row)

    def _on_filmstrip_reordered(self, ordered_ids: list[str]) -> None:
        selected_ids = {
            str(item.data(Qt.ItemDataRole.UserRole)) for item in self.filmstrip.selectedItems()
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
        self._filmstrip_items = {
            str(self.filmstrip.item(row).data(Qt.ItemDataRole.UserRole)): self.filmstrip.item(row)
            for row in range(self.filmstrip.count())
        }
        if not WORKSPACE.reorder_images(ordered_ids):
            self._filmstrip_project_id = ""
            if WORKSPACE.current is not None:
                self.refresh(WORKSPACE.current)
            return
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
        self.selected_button.setText(f"Translate Selected ({len(eligible)})" if eligible else "Translate Selected")
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
        completed = {
            image.id for image in WORKSPACE.current.images
            if image.id in all_selected and image.status in {"ready", "review"}
        } if WORKSPACE.current else set()
        retranslate = menu.addAction(f"Retranslate Completed ({len(completed)})")
        retranslate.setEnabled(bool(completed) and not APP_STATE.busy)
        retranslate.triggered.connect(lambda: self._retranslate_selected(completed))
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
            if image.id in selected and image.status in {"pending", "queued", "partial", "failed", "cancelled"}
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
            QMessageBox.warning(self, f"Could not delete {noun}", str(error))
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
        self._update_color_swatch(edit.color)
        alignment_index = self.alignment.findData(edit.alignment); self.alignment.setCurrentIndex(max(0, alignment_index))
        selected_type = edit.bubble_type or group.get("bubble_type", "dialogue")
        self._sync_bubble_type_options(str(selected_type))
        bubble_index = self.bubble_type.findData(selected_type); self.bubble_type.setCurrentIndex(max(0, bubble_index))
        self.offset_x.setValue(edit.offset_x); self.offset_y.setValue(edit.offset_y)
        self._update_ocr_queue_status()

    @staticmethod
    def _layout_from_group(group: dict) -> dict | None:
        layout = group.get("text_layout")
        if isinstance(layout, dict):
            try:
                return {
                    "x": int(layout["x"]),
                    "y": int(layout["y"]),
                    "width": int(layout["width"]),
                    "height": int(layout["height"]),
                }
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

    def _text_layout_changed(self, row: int, layout: dict) -> None:
        if not (0 <= row < len(self._groups)):
            return
        group = self._groups[row]
        key = self._layout_key(APP_STATE.selected_image, group["index"])
        before = self._pending_text_layouts.get(key) or self._layout_from_group(group)
        after = {key: int(layout[key]) for key in ("x", "y", "width", "height")}
        if before == after:
            return
        command = {
            "image_index": APP_STATE.selected_image,
            "group_index": group["index"],
            "row": row,
            "before": before,
            "after": after,
        }
        if self._apply_text_layout_command(command, after):
            self._layout_undo.append(command)
            self._layout_redo.clear()

    def _apply_text_layout_command(self, command: dict, layout: dict | None) -> bool:
        if layout is None:
            return False
        image_index = int(command["image_index"])
        group_index = command["group_index"]
        row = int(command.get("row", APP_STATE.selected_block))
        staged = {key: int(layout[key]) for key in ("x", "y", "width", "height")}
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
        if reasons:
            label += f" • {reasons}"
        self.confidence.setText(label)
        self.confidence_bar.setValue(round(ocr * 1000))

    def _select_block(self, row: int) -> None:
        if row < 0: return
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
        self.next_ocr_issue.setText(f"Next OCR Issue ({count})" if count else "Next OCR Issue")
        self.next_ocr_issue.setEnabled(count > 0 and not APP_STATE.busy)
        review_count = len(WORKSPACE.review_issue_queue())
        self.next_review_issue.setText(f"Next Review Issue ({review_count})" if review_count else "Next Review Issue")
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

    def _on_selection(self, image: int, block: int) -> None:
        if (
            image >= 0
            and self._ignore_next_open_page_selection
            and self.identity_tile.isChecked()
            and self.canvas_stack.currentWidget() is self.identity_preview
        ):
            self._ignore_next_open_page_selection = False
            APP_STATE.selected_image = -1
            APP_STATE.selected_block = -1
            self._clear_filmstrip_current()
            self._show_identity_workspace()
            return
        if image >= 0:
            self._ignore_next_open_page_selection = False
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
        row = APP_STATE.selected_block
        if row < 0 or row >= len(self._groups): return
        group = self._groups[row]
        existing = WORKSPACE.current.images[APP_STATE.selected_image].edits.get(str(group["index"]), RegionEdit()) if WORKSPACE.current else RegionEdit()
        pending_layout = self._pending_text_layouts.get(self._layout_key(APP_STATE.selected_image, group["index"]))
        edit = RegionEdit(
            translated_text=self.translation.toPlainText(),
            replace=self.replace.isChecked(),
            font_size=0 if pending_layout else self.font_size.value(),
            offset_x=self.offset_x.value(),
            offset_y=self.offset_y.value(),
            font_family=self.font.currentText(),
            color=self.color.text(),
            alignment=self.alignment.currentData(),
            original_text=self.original_text.toPlainText(),
            bubble_type=self.bubble_type.currentData(),
            layout_x=pending_layout["x"] if pending_layout else existing.layout_x,
            layout_y=pending_layout["y"] if pending_layout else existing.layout_y,
            layout_width=pending_layout["width"] if pending_layout else existing.layout_width,
            layout_height=pending_layout["height"] if pending_layout else existing.layout_height,
        )
        working = self._show_working("Apply & Rerender", "Preparing selected bubble...")
        try:
            working.set_message("Validating text fit and render settings...")
            WORKSPACE.validate_edit(APP_STATE.selected_image, group["index"], edit)
            working.set_message("Saving edits and learning drafts...")
            WORKSPACE.update_edit(APP_STATE.selected_image, group["index"], edit)
            working.set_message("Rendering the translated page...")
            WORKSPACE.rerender_image(APP_STATE.selected_image, log_callback=working.append_log)
            working.set_message("Refreshing the editor preview...")
            self._pending_text_layouts.pop(self._layout_key(APP_STATE.selected_image, group["index"]), None)
            self._load_image(APP_STATE.selected_image, row)
            self.status.setText("Text style applied and page rerendered")
        except (MemoryError, OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._close_working(working); working = None
            QMessageBox.warning(self, "Could not render", str(error))
        finally:
            self._close_working(working)

    def _approve_ai_block(self) -> None:
        row = APP_STATE.selected_block
        if not (0 <= row < len(self._groups)):
            return
        group = self._groups[row]
        answer = QMessageBox.question(
            self,
            "Approve Bubble",
            "Approve the selected bubble and record its review outcome for learning?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        working = self._show_working("Approve Bubble", "Capturing review outcome...")
        try:
            working.set_message("Sending bubble approval to Hydra AI...")
            summary = WORKSPACE.approve_ai_block(APP_STATE.selected_image, group["index"])
            if summary is None:
                self._close_working(working); working = None
                QMessageBox.warning(self, "Hydra AI", HYDRA_AI.error or "HydraMangaAi is unavailable.")
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
        answer = QMessageBox.question(
            self, "Approve page OCR",
            f"Approve all OCR/bubble review items on this page ({len(page_items)} block(s))?\n\n"
            "Only captured corrections become training samples; unchanged outputs remain review outcomes.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        working = self._show_working("Approve Page OCR", "Capturing OCR/bubble review outcomes...")
        try:
            working.set_message("Sending OCR/bubble approvals to Hydra AI...")
            summary = WORKSPACE.approve_ai_page_bubbles(APP_STATE.selected_image)
            if summary is None:
                self._close_working(working); working = None
                QMessageBox.warning(self, "Hydra AI", HYDRA_AI.error or "HydraMangaAi is unavailable.")
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
        answer = QMessageBox.question(
            self, "Approve page review",
            f"Approve all non-OCR review items on this page ({len(page_items)} block(s))?\n\n"
            "Only captured corrections become training samples; unchanged outputs remain review outcomes.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        working = self._show_working("Approve Page Review", "Capturing page review outcomes...")
        try:
            working.set_message("Sending review approvals to Hydra AI...")
            summary = WORKSPACE.approve_ai_page_reviews(APP_STATE.selected_image)
            if summary is None:
                self._close_working(working); working = None
                QMessageBox.warning(self, "Hydra AI", HYDRA_AI.error or "HydraMangaAi is unavailable.")
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
        if not WORKSPACE.request_manual_region(APP_STATE.selected_image, rect):
            self.status.setText("Manual translation is already running")

    def _manual_region_created(self, polygon: list[list[int]]) -> None:
        kind = self._manual_creation_kind
        self._reset_region_tool()
        self._manual_creation_kind = "region"
        if kind == "title":
            if not WORKSPACE.request_title_region(APP_STATE.selected_image, polygon):
                self.status.setText("Could not create title reconstruction region")
            return
        if not WORKSPACE.request_manual_region(APP_STATE.selected_image, polygon):
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
        if image_index != APP_STATE.selected_image:
            return
        self._load_image(image_index)
        row = next((index for index, group in enumerate(self._groups) if str(group.get("index")) == key), -1)
        if row >= 0:
            APP_STATE.select(image_index, row)
        group = self._groups[row] if row >= 0 else {}
        self.status.setText("Title reconstruction region created" if group.get("bubble_type") == "title" else "Manual text box translated")

    def _on_manual_region_failed(self, image_index: int, message: str) -> None:
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

    def _remove_selected_block(self) -> None:
        row = APP_STATE.selected_block
        if not (0 <= row < len(self._groups)):
            return
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
        working = self._show_working("Remove Text Block", "Updating the translated page...")
        try:
            if is_manual:
                working.set_message("Deleting the manual block and restoring covered regions...")
                removed = WORKSPACE.delete_manual_region(APP_STATE.selected_image, str(group["index"]))
                message = "Manual text box deleted; its automatic blocks were restored"
            else:
                working.set_message("Removing the automatic block...")
                removed = WORKSPACE.suppress_auto_region(APP_STATE.selected_image, int(group["index"]))
                message = "Automatic block removed; draw an Add Text Box replacement if needed"
            if removed:
                working.set_message("Refreshing the editor preview...")
                self._load_image(APP_STATE.selected_image, max(-1, row - 1))
                self.status.setText(message)
                success_message = message
        except (MemoryError, OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._close_working(working); working = None
            QMessageBox.warning(self, "Could not remove text block", str(error))
        finally:
            self._close_working(working)
        if success_message:
            QMessageBox.information(self, "Bubble deleted", success_message)

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
        if SettingsDialog(self).exec() == QDialog.DialogCode.Accepted:
            self._configure_manual_shortcut()
            if WORKSPACE.current is not None:
                project_id = str(getattr(WORKSPACE.current, "id", WORKSPACE.current.name))
                self._apply_filmstrip_collapse_preference(WORKSPACE.current, project_id)
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
        pending = bool(WORKSPACE.current and any(image.status in {"pending", "queued", "partial", "failed", "cancelled"} for image in WORKSPACE.current.images))
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

    def _export(self) -> None:
        dialog = ExportOptionsDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        output_type = str(dialog.output_type.currentData())
        image_format = str(dialog.image_format.currentData())
        if output_type == "folder":
            folder = QFileDialog.getExistingDirectory(self, "Export image folder")
            if not folder:
                return
            try:
                count = WORKSPACE.export(Path(folder), image_format=image_format)
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, "Export failed", str(error)); return
            QMessageBox.information(self, "Export complete", f"Exported {count} image(s).")
            return

        archive_format = "cbz" if output_type == "cbz" else "zip"
        filter_label = "CBZ comic archive (*.cbz)" if archive_format == "cbz" else "ZIP archive (*.zip)"
        path, _ = QFileDialog.getSaveFileName(self, "Export archive", "", f"{filter_label};;All files (*.*)")
        if not path:
            return
        try:
            archive = WORKSPACE.export_archive(Path(path), image_format=image_format, archive_format=archive_format)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Export failed", str(error)); return
        if archive is not None:
            QMessageBox.information(self, "Export complete", f"Exported archive:\n{archive}")
