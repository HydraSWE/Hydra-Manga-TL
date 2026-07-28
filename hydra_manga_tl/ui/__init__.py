"""Compatibility exports for the split Hydra Manga TL UI modules."""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from hydra_manga_tl.core.settings import CREDENTIALS, SETTINGS
from hydra_manga_tl.ui.canvas import CanvasView, PolygonVertexHandle
from hydra_manga_tl.ui.dialogs import (
    AiCenterDialog,
    ExportOptionsDialog,
    GlossaryDialog,
    IdentityPreviewDialog,
    SettingsDialog,
    TranslationTestWorker,
    WorkingDialog,
)
from hydra_manga_tl.ui.filmstrip import ReorderableFilmstrip
from hydra_manga_tl.ui.landing import DropZone, ImportProgressScreen, LandingScreen, RecentProjectCard, RecentProjectsScrollArea
from hydra_manga_tl.ui.main import MainWindow
from hydra_manga_tl.ui.shared import (
    FILMSTRIP_CARD_SIZE,
    FILMSTRIP_PREVIEW_SIZE,
    TARGET_LANGUAGE_NAMES,
    CollapsibleSection,
    _landing_icon,
    _language_badge,
    _page_label,
    _relative_opened_label,
    _speaker_icon,
)
from hydra_manga_tl.ui.workspace import WorkspaceScreen

__all__ = [
    "AiCenterDialog",
    "CanvasView",
    "CREDENTIALS",
    "CollapsibleSection",
    "DropZone",
    "ExportOptionsDialog",
    "FILMSTRIP_CARD_SIZE",
    "FILMSTRIP_PREVIEW_SIZE",
    "GlossaryDialog",
    "IdentityPreviewDialog",
    "ImportProgressScreen",
    "LandingScreen",
    "MainWindow",
    "QMessageBox",
    "PolygonVertexHandle",
    "RecentProjectCard",
    "RecentProjectsScrollArea",
    "ReorderableFilmstrip",
    "SettingsDialog",
    "SETTINGS",
    "TARGET_LANGUAGE_NAMES",
    "TranslationTestWorker",
    "WorkingDialog",
    "WorkspaceScreen",
    "_landing_icon",
    "_language_badge",
    "_page_label",
    "_relative_opened_label",
    "_speaker_icon",
]
