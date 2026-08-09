"""Secondary dialogs for the Hydra Manga TL UI.

This package preserves the original ``hydra_manga_tl.ui.dialogs`` import path
while keeping each dialog implementation in a focused module.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from hydra_manga_tl.core.paths import PATHS
from hydra_manga_tl.translation.memory import TRANSLATION_MEMORY

from .ai_center import AiCenterDialog
from .background_work import BackgroundWorkDialog
from .export import ExportOptionsDialog
from .glossary import GlossaryDialog
from .gpu import GpuDiagnosticsWorker
from .identity_preview import IdentityPreviewDialog
from .phrase_memory import PhraseMemoryManagerDialog
from .settings import SettingsDialog
from .translation_test import TranslationTestWorker
from .working import WorkingDialog

__all__ = [
    "AiCenterDialog",
    "BackgroundWorkDialog",
    "ExportOptionsDialog",
    "GlossaryDialog",
    "GpuDiagnosticsWorker",
    "IdentityPreviewDialog",
    "PATHS",
    "PhraseMemoryManagerDialog",
    "QMessageBox",
    "SettingsDialog",
    "TranslationTestWorker",
    "TRANSLATION_MEMORY",
    "WorkingDialog",
]
