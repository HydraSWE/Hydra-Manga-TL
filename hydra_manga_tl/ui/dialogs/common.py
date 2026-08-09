"""Shared imports and helpers for secondary UI dialogs."""

from __future__ import annotations

from pathlib import Path
import time
import zipfile

from PySide6.QtCore import QObject, QSize, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog,
    QKeySequenceEdit, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget
)

from hydra_manga_tl import __version__
from hydra_manga_tl.core.ai_bridge import HYDRA_AI
from hydra_manga_tl.core.diagnostics import create_diagnostics_bundle
from hydra_manga_tl.core.gpu import (
    GpuDiagnostic,
    collect_gpu_diagnostics,
)
from hydra_manga_tl.core.paths import PATHS, AppPaths
from hydra_manga_tl.core.settings import CREDENTIALS, SETTINGS
from hydra_manga_tl.core.updater import (
    STATUS_AVAILABLE,
    STATUS_CHECKING,
    STATUS_FAILED,
    STATUS_UP_TO_DATE,
    UPDATER,
    UpdateState,
)
from hydra_manga_tl.core.user_errors import (
    data_folder_error,
    diagnostics_error,
    manual_translation_error,
    memory_transfer_error,
    settings_error,
)
from hydra_manga_tl.translation.engines.model_manager import (
    KNOWN_MODEL_PACKAGES,
    ModelPackage,
    scan_local_qwen_models,
)
from hydra_manga_tl.translation.memory import TRANSLATION_MEMORY
from hydra_manga_tl.translation.phrase_memory import PHRASE_MEMORY
from hydra_manga_tl.translation.scheduler import (
    DEFAULT_PROVIDER_PROFILES,
    resolve_provider_worker_count,
)
from hydra_manga_tl.project.workspace import WORKSPACE
from hydra_manga_tl.ui.shared import lucide_icon


PROVIDER_BADGES = {
    "local": ("L", "#6b7280"),
    "openai": ("O", "#2f855a"),
    "openai_compatible": ("C", "#6d28d9"),
    "gemini": ("G", "#3b82f6"),
    "groq": ("Gr", "#e24a3b"),
    "deepseek": ("Ds", "#2563eb"),
    "google": ("Go", "#f2c94c"),
    "marian": ("Mt", "#2aa198"),
    "qwen": ("Q", "#4f46e5"),
}

APP_AUTHOR = "HydraSWE"
APP_WEBSITE_URL = "https://hydramangatl.annomous.com"
APP_GITHUB_URL = "https://github.com/HydraSWE/Hydra-Manga-TL"


def _provider_icon(provider: str) -> QIcon:
    text, color = PROVIDER_BADGES.get(provider, ("?", "#6b7280"))
    pixmap = QPixmap(22, 22)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(1, 1, 20, 20, 4, 4)
    painter.setPen(QColor("#ffffff"))
    font = painter.font()
    font.setBold(True)
    font.setPointSize(7 if len(text) > 1 else 9)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    return QIcon(pixmap)


def _add_provider_item(combo: QComboBox, label: str, provider: str) -> None:
    combo.addItem(_provider_icon(provider), label, provider)


