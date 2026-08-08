"""Secondary dialogs for the Hydra Manga TL UI."""

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


class AiCenterDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Hydra AI Dataset Dashboard")
        self.resize(800, 760)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        
        title = QLabel("Hydra AI Center")
        title.setObjectName("Heading")
        layout.addWidget(title)
        
        self.profile_label = QLabel()
        self.profile_label.setObjectName("Muted")
        layout.addWidget(self.profile_label)
        
        intro = QLabel("Dataset readiness • Japanese → English • only explicitly approved corrections count toward training")
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        layout.addWidget(intro)
        
        self.training_state = QLabel()
        self.training_state.setWordWrap(True)
        layout.addWidget(self.training_state)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        
        host = QWidget()
        self.cards_layout = QVBoxLayout(host)
        self.cards_layout.setContentsMargins(0, 8, 8, 8)
        self.cards_layout.setSpacing(12)
        
        self.cards = {}
        tasks = (
            ("OCR Expert", "ocr"), 
            ("Translation Expert", "translation"), 
            ("Bubble Detector", "bubble"),
            ("Layout Expert", "layout"), 
            ("Image Cleaner", "cleaner"), 
            ("Quality Judge", "quality")
        )
        
        for label, task in tasks:
            card = QFrame()
            card.setObjectName("ProgressPanel")
            
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(20, 16, 20, 16)
            card_layout.setSpacing(8)
            
            heading_row = QHBoxLayout()
            heading = QLabel(label)
            heading.setObjectName("JobTitle")
            
            count = QLabel("0 / 0")
            count.setObjectName("Muted")
            
            heading_row.addWidget(heading)
            heading_row.addStretch()
            heading_row.addWidget(count)
            
            progress = QProgressBar()
            progress.setRange(0, 1)
            progress.setValue(0)
            progress.setTextVisible(True)
            progress.setFixedHeight(18)
            
            detail = QLabel("Waiting for approved corrections")
            detail.setObjectName("Muted")
            detail.setWordWrap(True)
            
            button_row = QHBoxLayout()
            button_row.setContentsMargins(0, 8, 0, 0)
            
            dry_run = QPushButton("Dry Run")
            dry_run.setCursor(Qt.CursorShape.PointingHandCursor)
            dry_run.clicked.connect(lambda _=False, value=task: self._dry_run(value))
            
            button = QPushButton("Not Ready")
            button.setEnabled(False)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, value=task: self._queue(value))
            
            button_row.addWidget(dry_run)
            button_row.addStretch()
            button_row.addWidget(button)
            
            card_layout.addLayout(heading_row)
            card_layout.addWidget(progress)
            card_layout.addWidget(detail)
            card_layout.addLayout(button_row)
            
            self.cards_layout.addWidget(card)
            self.cards[task] = {
                "count": count, 
                "progress": progress, 
                "detail": detail, 
                "button": button, 
                "dry_run": dry_run
            }
            
        self.cards_layout.addStretch()
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)
        
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 8, 0, 0)
        
        refresh = QPushButton("Refresh")
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.clicked.connect(self.refresh)
        
        pause = QPushButton("Pause Training")
        pause.setCursor(Qt.CursorShape.PointingHandCursor)
        pause.clicked.connect(self._pause)
        
        resume = QPushButton("Resume Training")
        resume.setCursor(Qt.CursorShape.PointingHandCursor)
        resume.clicked.connect(self._resume)
        
        close = QPushButton("Close")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.accept)
        
        controls.addWidget(refresh)
        controls.addWidget(pause)
        controls.addWidget(resume)
        controls.addStretch()
        controls.addWidget(close)
        
        layout.addLayout(controls)
        self.refresh()

    def refresh(self) -> None:
        payload = HYDRA_AI.model_status()
        style = WORKSPACE.current.text_style if WORKSPACE.current else "Manga"
        self.profile_label.setText(f"Style profile: {style} • Data root: {SETTINGS.ai_data_root}")
        paused = bool(payload.get("paused"))
        self.training_state.setText("Training is paused." if paused else "Training is available only when a model reaches both dataset and golden-set thresholds.")
        progress_data = payload.get("progress", {})
        
        for task, widgets in self.cards.items():
            profile = style if task in {"translation", "layout"} else "global"
            item = progress_data.get(f"{task}:{profile}", {})
            approved = int(item.get("approved", 0))
            required = max(1, int(item.get("required", 1)))
            unit = item.get("unit", "samples")
            
            widgets["count"].setText(f"{approved:,} / {required:,} {unit}")
            widgets["progress"].setRange(0, required)
            widgets["progress"].setValue(min(approved, required))
            widgets["progress"].setFormat(f"%p%  •  %v / {required:,}")
            
            golden = int(item.get("golden", 0))
            required_golden = int(item.get("required_golden", 0))
            details = [f"Golden set: {golden:,} / {required_golden:,}"]
            
            if task == "bubble":
                details.append(f"Approved regions: {int(item.get('regions', 0)):,} / {int(item.get('required_regions', 2000)):,}")
            reasons = item.get("reasons", [])
            
            if reasons:
                details.append("Blocked: " + "; ".join(reasons[:2]))
            elif item.get("ready"):
                details.append("Ready to train")
                
            widgets["detail"].setText(" • ".join(details))
            ready = bool(item.get("ready")) and not paused
            widgets["button"].setEnabled(ready)
            widgets["button"].setText("Queue Training" if ready else "Not Ready")

    def _queue(self, task: str) -> None:
        style = WORKSPACE.current.text_style if WORKSPACE.current else "Manga"
        run_id = HYDRA_AI.queue_training(task, style)
        self.refresh()
        QMessageBox.information(self, "Training queue", f"Run recorded: {run_id}" if run_id else "HydraMangaAi is unavailable.")

    def _dry_run(self, task: str) -> None:
        style = WORKSPACE.current.text_style if WORKSPACE.current else "Manga"
        result = HYDRA_AI.training_dry_run(task, style)
        readiness = result.get("readiness", {})
        reasons = readiness.get("reasons") or result.get("reasons") or ()
        
        lines = [
            f"Task: {result.get('task', task)}",
            f"Profile: {result.get('profile', style)}",
            f"Ready: {'yes' if result.get('ready') else 'no'}",
        ]
        if readiness:
            lines.extend([
                f"Approved pairs: {readiness.get('approved_pairs', 0):,}",
                f"Golden pairs: {readiness.get('golden_pairs', 0):,}",
                f"Projects: {readiness.get('project_count', 0):,}",
            ])
        if reasons:
            lines.append("Blocked: " + "; ".join(str(reason) for reason in reasons))
        if result.get("next_step"):
            lines.append(str(result["next_step"]))
            
        QMessageBox.information(self, "Training dry run", "\n".join(lines))

    def _pause(self) -> None:
        HYDRA_AI.pause_training()
        self.refresh()

    def _resume(self) -> None:
        HYDRA_AI.resume_training()
        self.refresh()


class WorkingDialog(QDialog):
    def __init__(self, title: str, message: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("WorkingDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setFixedWidth(480)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)
        
        heading = QLabel(title)
        heading.setObjectName("WorkingTitle")
        heading.setStyleSheet("font-weight: bold; font-size: 14pt;")
        
        self.message = QLabel(message)
        self.message.setObjectName("Muted")
        self.message.setWordWrap(True)
        
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(12)
        
        self.log = QTextEdit()
        self.log.setObjectName("WorkingLog")
        self.log.setReadOnly(True)
        self.log.setFixedHeight(140)
        self.log.hide()
        
        layout.addWidget(heading)
        layout.addWidget(self.message)
        layout.addWidget(self.progress)
        layout.addWidget(self.log)

    def set_message(self, message: str) -> None:
        self.message.setText(message)
        QApplication.processEvents()

    def append_log(self, message: str) -> None:
        line = message.strip()
        if not line:
            return
        self.log.show()
        self.log.append(line)
        scrollbar = self.log.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        QApplication.processEvents()


class ExportOptionsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export")
        self.setModal(True)
        self.setFixedWidth(420)

        self.output_type = QComboBox()
        self.output_type.addItem("Image folder", "folder")
        self.output_type.addItem("ZIP archive", "zip")
        self.output_type.addItem("CBZ comic archive", "cbz")
        self.output_type.addItem("PDF document", "pdf")

        self.image_format = QComboBox()
        self.image_format.addItem("PNG", "png")
        self.image_format.addItem("JPEG", "jpg")
        self.image_format.addItem("WebP", "webp")
        self.output_type.currentIndexChanged.connect(
            lambda: self.image_format.setEnabled(
                self.output_type.currentData() != "pdf"
            )
        )

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        form.addRow("Output", self.output_type)
        form.addRow("Image format", self.image_format)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Export")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(20)
        layout.addLayout(form)
        layout.addWidget(buttons)


class IdentityPreviewDialog(QDialog):
    def __init__(self, image_path: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Hydra Manga TL")
        self.resize(760, 800)
        self.setMinimumSize(420, 460)
        self._source = QPixmap(str(image_path))
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)
        
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setObjectName("IdentityPreview")
        layout.addWidget(self.preview, 1)
        
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        
        self._scale_preview()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._scale_preview()

    def _scale_preview(self) -> None:
        if self._source.isNull():
            self.preview.setText("Hydra identity artwork is unavailable.")
            return
        available = self.preview.size() - QSize(16, 16)
        if available.width() <= 0 or available.height() <= 0:
            return
        self.preview.setPixmap(
            self._source.scaled(
                available,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


class TranslationTestWorker(QObject):
    completed = Signal(bool, str)

    def __init__(
        self,
        *,
        qwen_model_path: str | None,
        preferred_engine: str,
        fallback_engine: str | None,
        qwen_model_name: str,
        provider_models: dict[str, str],
        provider_base_urls: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.qwen_model_path = qwen_model_path
        self.preferred_engine = preferred_engine
        self.fallback_engine = fallback_engine
        self.qwen_model_name = qwen_model_name
        self.provider_models = provider_models
        self.provider_base_urls = provider_base_urls or {}
        self._cancel_requested = False
        self._manager = None

    def cancel(self) -> None:
        self._cancel_requested = True
        manager = self._manager
        if manager is not None:
            for engine in tuple(getattr(manager, "engines", {}).values()):
                cancel = getattr(engine, "cancel", None)
                if callable(cancel):
                    cancel()

    def _cancelled(self) -> bool:
        return self._cancel_requested or QThread.currentThread().isInterruptionRequested()

    @Slot()
    def run(self) -> None:
        from hydra_manga_tl.translation.engines import PageDialogue, TranslationEngineManager

        manager = TranslationEngineManager(
            glossary={},
            qwen_model_path=self.qwen_model_path,
            preferred_engine=self.preferred_engine,
            fallback_engine=self.fallback_engine,
            qwen_model_name=self.qwen_model_name,
            provider_models=self.provider_models,
            provider_base_urls=self.provider_base_urls,
            allow_local_fallback_for_cloud=True,
            translation_memory_enabled=False,
        )
        self._manager = manager
        page = PageDialogue(
            source_language="Japanese",
            target_language="en",
            dialogue=[{"id": "r1", "text": "待て！"}],
        )
        started = time.perf_counter()
        try:
            if self._cancelled():
                return
            manager.load()
            if self._cancelled():
                return
            result = manager.translate_page(page)
            if self._cancelled():
                return
            sample = str(result.translations[0].get("text", "")) if result.translations else ""
            reported_engine = getattr(manager, "last_engine_id", "")
            engine_key = (
                reported_engine
                if isinstance(reported_engine, str) and reported_engine
                else self.preferred_engine
            )
            engine = manager.engines.get(engine_key)
            self.completed.emit(
                True,
                self._diagnostic_message(
                    engine_key,
                    engine,
                    elapsed=time.perf_counter() - started,
                    sample=sample,
                ),
            )
        except Exception as error:
            if self._cancelled():
                return
            engine = manager.engines.get(self.preferred_engine)
            self.completed.emit(
                False,
                self._diagnostic_message(
                    self.preferred_engine,
                    engine,
                    elapsed=time.perf_counter() - started,
                    error=error,
                ),
            )
        finally:
            manager.unload()
            self._manager = None

    def _diagnostic_message(
        self,
        engine_key: str,
        engine,
        *,
        elapsed: float,
        sample: str = "",
        error: Exception | None = None,
    ) -> str:
        backend = "llama.cpp" if engine_key == "qwen" else (
            "Transformers / MarianMT" if engine_key == "marian" else engine_key
        )
        model_path = self.qwen_model_path or "Not configured"
        device = "Unknown"
        offload = "Not applicable"
        if engine_key == "qwen":
            runtime_config = dict(getattr(engine, "runtime_config", {}) or {})
            gpu_layers = int(runtime_config.get("n_gpu_layers", 0) or 0)
            offload = (
                "CPU only"
                if gpu_layers == 0
                else "Automatic GPU offload"
                if gpu_layers < 0
                else f"{gpu_layers} GPU layer(s)"
            )
            device = "CPU" if gpu_layers == 0 else "GPU offload requested"
            model_path = str(getattr(engine, "model_path", "") or model_path)
        elif engine_key == "marian":
            model_path = "Helsinki-NLP model cache"
            try:
                import torch
                if torch.cuda.is_available():
                    device = torch.cuda.get_device_name(0)
                else:
                    device = "CPU"
            except (ImportError, RuntimeError):
                device = "Unavailable"

        lines = [
            f"Requested engine: {self.preferred_engine}",
            f"Engine used: {engine_key}",
            f"Backend: {backend}",
            f"Device: {device}",
            f"Model path: {model_path}",
            f"Configured offload: {offload}",
            f"Native load: {'Passed' if error is None else 'Failed'}",
            f"Elapsed: {elapsed:.2f}s",
        ]
        gpu_report = collect_gpu_diagnostics(run_load_test=False)
        lines.extend([
            f"GPU hardware: {gpu_report.device_name or gpu_report.status}",
            (
                f"VRAM: {gpu_report.memory_used_mb:,} MiB used / "
                f"{gpu_report.memory_total_mb:,} MiB"
                if gpu_report.memory_total_mb
                else "VRAM: unavailable"
            ),
            f"Driver: {gpu_report.driver_version or 'unavailable'}",
        ])
        for name, diagnostic in gpu_report.backends.items():
            lines.append(
                f"{name}: "
                f"{'GPU ready' if diagnostic.gpu_ready else diagnostic.detail or 'unavailable'}"
                f"{f' — {diagnostic.error}' if diagnostic.error else ''}"
            )
        if error is None:
            lines.append(f"Sample result: {sample or '[empty translation]'}")
        else:
            lines.append(manual_translation_error(error))
            if engine_key == "qwen":
                lines.append(
                    "Action: verify the GGUF path, llama/CUDA DLL availability, "
                    "and try CPU offload if the GPU runtime cannot load."
                )
            elif engine_key == "marian":
                lines.append(
                    "Action: verify the local model cache and Torch device installation."
                )
        return "\n".join(lines)


class PhraseMemoryManagerDialog(QDialog):
    """Phrase Memory (PM v1) Manager dialog for viewing, editing, verifying, deleting, importing, and exporting entries."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Phrase Memory Manager (PM v1)")
        self.resize(960, 640)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        header = QLabel("Phrase Memory (PM v1)")
        header.setObjectName("Heading")
        layout.addWidget(header)

        sub = QLabel("Deterministic sub-phrase memory learned from validated translations. Entries supply terminology hints to translation providers.")
        sub.setWordWrap(True)
        sub.setObjectName("Muted")
        layout.addWidget(sub)

        filter_layout = QHBoxLayout()
        filter_layout.setSpacing(12)
        filter_label = QLabel("Search:")
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter source or target phrase...")
        self.search_input.textChanged.connect(self._apply_filter)
        self.search_input.setFixedHeight(32)
        
        filter_layout.addWidget(filter_label)
        filter_layout.addWidget(self.search_input, 1)
        layout.addLayout(filter_layout)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["ID", "Source Phrase", "Target Phrase", "Uses", "Verified", "Origin"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.itemDoubleClicked.connect(self._edit_selected)
        
        # Modernizing the table look
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        
        layout.addWidget(self.table, 1)

        self.stats_label = QLabel()
        self.stats_label.setObjectName("Muted")
        layout.addWidget(self.stats_label)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)
        
        edit_btn = QPushButton("Edit Entry")
        edit_btn.clicked.connect(self._edit_selected)
        verify_btn = QPushButton("Toggle Verified")
        verify_btn.clicked.connect(self._toggle_verified_selected)
        delete_btn = QPushButton("Delete Entry")
        delete_btn.clicked.connect(self._delete_selected)
        import_btn = QPushButton("Import (.pmdb/.json)")
        import_btn.clicked.connect(self._import_file)
        export_btn = QPushButton("Export (.pmdb/.json)")
        export_btn.clicked.connect(self._export_file)
        clear_btn = QPushButton("Clear All")
        clear_btn.clicked.connect(self._clear_all)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        
        for btn in (edit_btn, verify_btn, delete_btn, import_btn, export_btn, clear_btn, close_btn):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumHeight(32)

        btn_layout.addWidget(edit_btn)
        btn_layout.addWidget(verify_btn)
        btn_layout.addWidget(delete_btn)
        btn_layout.addWidget(import_btn)
        btn_layout.addWidget(export_btn)
        btn_layout.addWidget(clear_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        self._all_entries = []
        self.refresh()

    def refresh(self) -> None:
        self._all_entries = PHRASE_MEMORY.all_entries()
        self._apply_filter()
        stats = PHRASE_MEMORY.statistics()
        self.stats_label.setText(
            f"Total entries: {stats.total_entries:,} · Verified: {stats.verified_entries:,} · Total matches: {stats.total_matches:,} · Learned: {stats.learned_count:,}"
        )

    def _apply_filter(self) -> None:
        query = self.search_input.text().strip().casefold()
        filtered = [
            e for e in self._all_entries
            if not query or query in e.source_phrase.casefold() or query in e.target_phrase.casefold()
        ]
        self.table.setRowCount(len(filtered))
        for row, entry in enumerate(filtered):
            id_item = QTableWidgetItem(str(entry.id))
            id_item.setData(Qt.ItemDataRole.UserRole, entry.id)
            src_item = QTableWidgetItem(entry.source_phrase)
            tgt_item = QTableWidgetItem(entry.target_phrase)
            uses_item = QTableWidgetItem(str(entry.usage_count))
            ver_item = QTableWidgetItem("✓ Verified" if entry.verified else "Unverified")
            orig_item = QTableWidgetItem(entry.origin)
            
            self.table.setItem(row, 0, id_item)
            self.table.setItem(row, 1, src_item)
            self.table.setItem(row, 2, tgt_item)
            self.table.setItem(row, 3, uses_item)
            self.table.setItem(row, 4, ver_item)
            self.table.setItem(row, 5, orig_item)

    def _selected_entry_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        id_item = self.table.item(row, 0)
        return id_item.data(Qt.ItemDataRole.UserRole) if id_item else None

    def _edit_selected(self) -> None:
        entry_id = self._selected_entry_id()
        if entry_id is None:
            QMessageBox.information(self, "Phrase Memory", "Select an entry to edit.")
            return
        entry = next((e for e in self._all_entries if e.id == entry_id), None)
        if entry is None:
            return
        new_target, ok = QInputDialog.getText(
            self,
            "Edit Phrase Memory Entry",
            f"Edit translation for '{entry.source_phrase}':",
            QLineEdit.EchoMode.Normal,
            entry.target_phrase,
        )
        if ok and new_target.strip():
            PHRASE_MEMORY.update_entry(entry_id, target_phrase=new_target.strip(), verified=True)
            self.refresh()

    def _toggle_verified_selected(self) -> None:
        entry_id = self._selected_entry_id()
        if entry_id is None:
            QMessageBox.information(self, "Phrase Memory", "Select an entry to toggle verification.")
            return
        PHRASE_MEMORY.toggle_verified(entry_id)
        self.refresh()

    def _delete_selected(self) -> None:
        entry_id = self._selected_entry_id()
        if entry_id is None:
            QMessageBox.information(self, "Phrase Memory", "Select an entry to delete.")
            return
        answer = QMessageBox.question(
            self,
            "Delete Entry",
            "Delete this Phrase Memory entry? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            PHRASE_MEMORY.delete_entry(entry_id)
            self.refresh()

    def _import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Phrase Memory",
            "",
            "Phrase Memory (*.pmdb *.json *.db *.sqlite *.sqlite3);;All files (*.*)",
        )
        if not path:
            return
        try:
            imported = PHRASE_MEMORY.import_file(Path(path))
            QMessageBox.information(self, "Phrase Memory Imported", f"Imported {imported:,} phrase memory entries.")
            self.refresh()
        except Exception as error:
            QMessageBox.warning(
                self,
                "Import Failed",
                memory_transfer_error(error, action="import", memory_name="Phrase Memory"),
            )

    def _export_file(self) -> None:
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export Phrase Memory",
            "",
            "Hydra PMDB (*.pmdb);;Hydra JSON (*.json);;Hydra SQLite (*.db)",
        )
        if not path:
            return
        suffix = Path(path).suffix.casefold()
        if not suffix:
            suffix = ".json" if "JSON" in selected_filter else ".pmdb"
            path += suffix
        try:
            destination = PHRASE_MEMORY.export(Path(path))
            QMessageBox.information(self, "Phrase Memory Exported", f"Exported Phrase Memory:\n{destination}")
        except Exception as error:
            QMessageBox.warning(
                self,
                "Export Failed",
                memory_transfer_error(error, action="export", memory_name="Phrase Memory"),
            )

    def _clear_all(self) -> None:
        answer = QMessageBox.question(
            self,
            "Clear Phrase Memory",
            "Delete every saved Phrase Memory entry and statistics? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            PHRASE_MEMORY.clear()
            self.refresh()


class GpuDiagnosticsWorker(QObject):
    """Background worker for non-blocking GPU hardware and backend diagnostics."""

    completed = Signal(object)

    def __init__(self, *, run_load_test: bool = False) -> None:
        super().__init__()
        self.run_load_test = run_load_test

    @Slot()
    def run(self) -> None:
        report = collect_gpu_diagnostics(run_load_test=self.run_load_test)
        self.completed.emit(report)


class SettingsDialog(QDialog):
    """Local-first provider preferences with secrets stored outside settings JSON."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Hydra Settings")
        self.setMinimumWidth(820)
        self.resize(1080, 800)
        
        self._gpu_thread: QThread | None = None
        self._gpu_worker: GpuDiagnosticsWorker | None = None
        self._test_thread: QThread | None = None
        self._test_worker: TranslationTestWorker | None = None
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.literal = QComboBox()
        _add_provider_item(self.literal, "MarianMT (Local)", "marian")
        _add_provider_item(self.literal, "Google Cloud Translation", "google")
        
        self.localization = QComboBox()
        for label, value in (
            ("Local manga cleanup", "local"),
            ("Local Qwen (optional)", "qwen"),
            ("OpenAI", "openai"),
            ("OpenAI-Compatible", "openai_compatible"),
            ("Gemini", "gemini"),
            ("Groq", "groq"),
            ("DeepSeek", "deepseek"),
        ):
            _add_provider_item(self.localization, label, value)
            
        self.translation_engine = QComboBox()
        _add_provider_item(self.translation_engine, "Groq", "groq")
        _add_provider_item(self.translation_engine, "OpenAI", "openai")
        _add_provider_item(self.translation_engine, "OpenAI-Compatible", "openai_compatible")
        _add_provider_item(self.translation_engine, "Google Translate", "google")
        _add_provider_item(self.translation_engine, "Gemini", "gemini")
        _add_provider_item(self.translation_engine, "Marian fallback", "marian")
        _add_provider_item(self.translation_engine, "Local Qwen (optional)", "qwen")
        
        self.translation_fallback = QComboBox()
        self.translation_fallback.addItem("No automatic fallback", "")
        _add_provider_item(self.translation_fallback, "Marian (local)", "marian")
        _add_provider_item(self.translation_fallback, "Groq", "groq")
        _add_provider_item(self.translation_fallback, "OpenAI", "openai")
        _add_provider_item(self.translation_fallback, "OpenAI-Compatible", "openai_compatible")
        _add_provider_item(self.translation_fallback, "Google Translate", "google")
        _add_provider_item(self.translation_fallback, "Gemini", "gemini")
        
        self.fast_workers = QSpinBox()
        self.fast_workers.setRange(0, 6)
        self.fast_workers.setSpecialValueText("Auto")
        self.fast_workers.setValue(max(0, min(6, int(SETTINGS.fast_worker_override))))
        self.fast_workers.setToolTip(
            "Fast mode translation workers. Provider profiles cap this value "
            "to protect local engines and rate-limited APIs."
        )
        
        self.fast_worker_hint = QLabel()
        self.fast_worker_hint.setObjectName("Muted")
        self.fast_worker_hint.setWordWrap(True)
        
        self.translation_engine.currentIndexChanged.connect(self._refresh_fast_worker_hint)
        self.fast_workers.valueChanged.connect(self._refresh_fast_worker_hint)
        
        self.translate_titles = QCheckBox("Translate titles automatically")
        self.translate_sfx = QCheckBox("Translate SFX automatically")
        self.translate_signs = QCheckBox("Translate signs automatically")
        self.translate_credits = QCheckBox("Translate credits automatically")
        
        self.translation_memory_enabled = QCheckBox("Enable Translation Memory")
        self.translation_memory_auto_learn = QCheckBox("Automatically learn validated translations")
        self.translation_memory_store_edits = QCheckBox("Store user translation edits as verified")
        self.translation_memory_prefer_verified = QCheckBox("Prefer verified entries")
        
        self.translation_memory_similarity = QLabel("Exact only (100%)")
        self.translation_memory_stats = QLabel()
        self.translation_memory_stats.setObjectName("Muted")
        self.translation_memory_stats.setWordWrap(True)
        
        self.translation_memory_import = QPushButton("Import")
        self.translation_memory_import.clicked.connect(self._import_translation_memory)
        
        self.translation_memory_export = QPushButton("Export")
        self.translation_memory_export.clicked.connect(self._export_translation_memory)
        
        self.translation_memory_clear = QPushButton("Clear Memory")
        self.translation_memory_clear.clicked.connect(self._clear_translation_memory)
        
        memory_actions = QWidget()
        memory_actions_layout = QHBoxLayout(memory_actions)
        memory_actions_layout.setContentsMargins(0, 0, 0, 0)
        memory_actions_layout.setSpacing(8)
        memory_actions_layout.addWidget(self.translation_memory_import)
        memory_actions_layout.addWidget(self.translation_memory_export)
        memory_actions_layout.addWidget(self.translation_memory_clear)

        self.phrase_memory_enabled = QCheckBox("Enable Phrase Memory")
        self.phrase_memory_auto_learn = QCheckBox("Automatically learn phrases")
        self.phrase_memory_prefer_verified = QCheckBox("Prefer verified phrases")
        
        self.phrase_memory_stats = QLabel()
        self.phrase_memory_stats.setObjectName("Muted")
        self.phrase_memory_stats.setWordWrap(True)
        
        self.phrase_memory_manage = QPushButton("Manage Phrase Memory")
        self.phrase_memory_manage.clicked.connect(self._open_phrase_memory_manager)
        
        pm_actions = QWidget()
        pm_actions_layout = QHBoxLayout(pm_actions)
        pm_actions_layout.setContentsMargins(0, 0, 0, 0)
        pm_actions_layout.addWidget(self.phrase_memory_manage)

        self.debug_artifacts = QCheckBox("Write OCR crops and diagnostic overlays")

        # Notification checkboxes
        self.notif_enabled = QCheckBox("Enable desktop notifications")
        self.notif_enabled.setToolTip(
            "Show system tray notifications for long-running background tasks "
            "when Hydra is minimized or in the background."
        )
        self.notif_translation_completed = QCheckBox("Translation completed")
        self.notif_translation_failed = QCheckBox("Translation failed")
        self.notif_export_completed = QCheckBox("Export completed")
        self.notif_export_failed = QCheckBox("Export failed")
        self.notif_review_queue = QCheckBox("Review queue created")
        self.notif_build_finished = QCheckBox("Build finished  (coming soon)")
        self.notif_build_finished.setEnabled(False)
        self.notif_updates_available = QCheckBox("Notify when updates are available")
        self.notif_enabled.toggled.connect(self._refresh_notif_row_state)

        self.updates_check_automatically = QCheckBox("Check for updates automatically")
        self.updates_prompt_before_download = QCheckBox("Prompt before download")
        self.app_author = QLabel(APP_AUTHOR)
        self.app_author.setObjectName("Muted")
        self.app_github = QLabel(
            f'<a href="{APP_GITHUB_URL}">{APP_GITHUB_URL}</a>'
        )
        self.app_github.setObjectName("Muted")
        self.app_github.setOpenExternalLinks(True)
        self.app_github.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        self.app_version = QLabel(__version__)
        self.app_version.setObjectName("Muted")
        self.update_status = QLabel("Ready to check for updates.")
        self.update_status.setObjectName("Muted")
        self.update_status.setWordWrap(True)
        self.update_check_now = QPushButton("Check Now")
        self.update_check_now.setIcon(lucide_icon("refresh-cw"))
        self.update_check_now.clicked.connect(self._check_for_updates_now)
        self.update_download = QPushButton("Download Now")
        self.update_download.setObjectName("Primary")
        self.update_download.setIcon(lucide_icon("download"))
        self.update_download.clicked.connect(self._download_update)
        self.update_download.setEnabled(False)

        update_actions = QWidget()
        update_actions_layout = QHBoxLayout(update_actions)
        update_actions_layout.setContentsMargins(0, 0, 0, 0)
        update_actions_layout.setSpacing(8)
        update_actions_layout.addWidget(self.update_check_now)
        update_actions_layout.addWidget(self.update_download)
        update_actions_layout.addStretch()
        self.update_actions = update_actions

        self.gpu_status = QLabel("Not checked")
        self.gpu_status.setWordWrap(True)
        
        self.gpu_details = QTextEdit()
        self.gpu_details.setReadOnly(True)
        self.gpu_details.setFixedHeight(132)
        self.gpu_details.setPlainText("Click Test GPU runtime to check NVIDIA hardware and native backends.")
        
        self.gpu_test = QPushButton("Test GPU runtime")
        self.gpu_test.setToolTip(
            "Test Torch CUDA allocation, llama.cpp GPU offload dependencies, "
            "and Paddle CUDA capability."
        )
        self.gpu_test.clicked.connect(self._test_gpu_runtime)
        
        self.qwen_model = QComboBox()
        for package in KNOWN_MODEL_PACKAGES.values():
            self.qwen_model.addItem(package.label, package.key)
        for local_pkg in scan_local_qwen_models():
            self.qwen_model.addItem(local_pkg.label, local_pkg.key)
        self.qwen_model.addItem("Custom / External GGUF Model...", "custom_gguf")
        self.qwen_model.currentIndexChanged.connect(self._on_qwen_model_selected)
            
        self.qwen_model_path = QLineEdit(SETTINGS.qwen_model_path)
        self.qwen_model_path.setPlaceholderText("Path to a .gguf model")
        
        self.qwen_status = QLabel(SETTINGS.qwen_model_status or "Not installed")
        self.qwen_estimate = QLabel("Estimated download: not available")
        self.qwen_estimate.setWordWrap(True)
        
        self.qwen_browse = QPushButton("Browse")
        self.qwen_browse.clicked.connect(self._browse_qwen_model)
        
        self.qwen_download = QPushButton("Download Model")
        self.qwen_download.clicked.connect(self._download_qwen_model)
        
        self.qwen_test = QPushButton("Test local engine")
        self.qwen_test.clicked.connect(self._test_qwen_translation)
        
        qwen_layout = QHBoxLayout()
        qwen_layout.setContentsMargins(0, 0, 0, 0)
        qwen_layout.setSpacing(8)
        qwen_layout.addWidget(self.qwen_model_path)
        qwen_layout.addWidget(self.qwen_browse)
        
        self.gemini_model = QLineEdit(SETTINGS.gemini_model)
        self.groq_model = QLineEdit(SETTINGS.groq_model)
        self.deepseek_model = QLineEdit(SETTINGS.deepseek_model)
        self.openai_model = QLineEdit(SETTINGS.openai_model)
        self.openai_compatible_preset = QComboBox()
        self.openai_compatible_preset.addItem("Kimi / TokenRouter", "kimi_tokenrouter")
        self.openai_compatible_preset.addItem("Custom OpenAI-Compatible", "custom")
        self.openai_compatible_preset.currentIndexChanged.connect(self._apply_openai_compatible_preset)
        self.openai_compatible_name = QLineEdit(SETTINGS.openai_compatible_name)
        self.openai_compatible_base_url = QLineEdit(SETTINGS.openai_compatible_base_url)
        self.openai_compatible_model = QLineEdit(SETTINGS.openai_compatible_model)
        
        self.manual_shortcut = QKeySequenceEdit(QKeySequence(SETTINGS.manual_textbox_shortcut or "Ctrl+D"))
        self.title_reconstruction_shortcut = QKeySequenceEdit(QKeySequence(SETTINGS.title_reconstruction_shortcut or "Ctrl+F"))
        
        self.filmstrip_collapse_mode = QComboBox()
        self.filmstrip_collapse_mode.addItem("Current behavior", "current")
        self.filmstrip_collapse_mode.addItem("Always collapsed", "always_collapsed")
        
        self.app_data_root = QLineEdit(str(PATHS.root))
        self.app_data_root.setPlaceholderText(str(AppPaths.default_root().resolve()))
        
        self.app_data_browse = QPushButton("Browse")
        self.app_data_browse.clicked.connect(self._browse_app_data_root)
        self.app_data_default = QPushButton("Default")
        self.app_data_default.clicked.connect(self._reset_app_data_root)
        
        self.export_root = QLineEdit(SETTINGS.export_root)
        self.export_root.setPlaceholderText(str((Path.home() / "Hydra Manga TL Exports").resolve()))
        
        self.export_browse = QPushButton("Browse")
        self.export_browse.clicked.connect(self._browse_export_root)
        self.export_default = QPushButton("Default")
        self.export_default.clicked.connect(self._reset_export_root)
        
        self.project_import_root = QLineEdit(
            SETTINGS.project_import_root or str(PATHS.projects)
        )
        self.project_import_root.setPlaceholderText(str(PATHS.projects))
        self.project_import_browse = QPushButton("Browse")
        self.project_import_browse.clicked.connect(self._browse_project_import_root)
        self.project_import_default = QPushButton("Default")
        self.project_import_default.clicked.connect(self._reset_project_import_root)

        self.manga_import_root = QLineEdit(SETTINGS.manga_import_root)
        self.manga_import_root.setPlaceholderText(str(Path.home().resolve()))
        self.manga_import_browse = QPushButton("Browse")
        self.manga_import_browse.clicked.connect(self._browse_manga_import_root)
        self.manga_import_default = QPushButton("Default")
        self.manga_import_default.clicked.connect(self._reset_manga_import_root)
        
        self.diagnostics_bundle = QPushButton("Create diagnostics bundle")
        self.diagnostics_bundle.setToolTip(
            "Save logs, non-secret settings, runtime versions, and recent stage timings. "
            "Credentials and project images are not included."
        )
        self.diagnostics_bundle.clicked.connect(self._create_diagnostics_bundle)
        
        app_data_layout = QHBoxLayout()
        app_data_layout.setContentsMargins(0, 0, 0, 0)
        app_data_layout.setSpacing(8)
        app_data_layout.addWidget(self.app_data_root, 1)
        app_data_layout.addWidget(self.app_data_browse)
        app_data_layout.addWidget(self.app_data_default)
        
        export_layout = QHBoxLayout()
        export_layout.setContentsMargins(0, 0, 0, 0)
        export_layout.setSpacing(8)
        export_layout.addWidget(self.export_root, 1)
        export_layout.addWidget(self.export_browse)
        export_layout.addWidget(self.export_default)
        
        project_import_layout = QHBoxLayout()
        project_import_layout.setContentsMargins(0, 0, 0, 0)
        project_import_layout.setSpacing(8)
        project_import_layout.addWidget(self.project_import_root, 1)
        project_import_layout.addWidget(self.project_import_browse)
        project_import_layout.addWidget(self.project_import_default)

        manga_import_layout = QHBoxLayout()
        manga_import_layout.setContentsMargins(0, 0, 0, 0)
        manga_import_layout.setSpacing(8)
        manga_import_layout.addWidget(self.manga_import_root, 1)
        manga_import_layout.addWidget(self.manga_import_browse)
        manga_import_layout.addWidget(self.manga_import_default)
        
        self.keys = {}
        for provider in ("google", "gemini", "groq", "deepseek", "openai", "openai_compatible"):
            field = QLineEdit()
            field.setEchoMode(QLineEdit.EchoMode.Password)
            field.setPlaceholderText("Stored securely" if CREDENTIALS.get(provider) else "Not configured")
            self.keys[provider] = field
            
        self.literal.setToolTip("Manual text-box literal pass. Batch pages use Batch translation engine.")
        self.localization.setToolTip("Primary engine used by manual text boxes.")
        self.translation_engine.setToolTip("Provider used by Translate All Pending and selected-page retranslation.")
        self.translation_fallback.setToolTip("Used when the selected automatic or manual engine supports fallback.")
        self.manual_shortcut.setToolTip("Keyboard shortcut for Region Tool in the workspace.")
        self.title_reconstruction_shortcut.setToolTip("Keyboard shortcut for Title Reconstruction in the workspace.")
        self.filmstrip_collapse_mode.setToolTip("Controls how the workspace Filmstrip opens when projects are shown.")
        self.app_data_root.setToolTip("Folder for Hydra projects, logs, caches, and Translation Memory.")
        self.export_root.setToolTip("Default folder shown when exporting images, PDFs, ZIP, or CBZ files.")
        self.project_import_root.setToolTip("Folder scanned when Open Project lists available Hydra projects.")
        self.manga_import_root.setToolTip("Default folder shown by the landing-page Import Manga button.")
        self.openai_compatible_base_url.setToolTip("Base URL for OpenAI-compatible providers; Hydra appends /chat/completions.")
        
        def make_section(title: str, rows: tuple[tuple[str, object], ...], description: str = "") -> QFrame:
            section = QFrame()
            section.setObjectName("ProgressPanel")
            section_layout = QVBoxLayout(section)
            section_layout.setContentsMargins(16, 16, 16, 16)
            section_layout.setSpacing(12)
            
            heading = QLabel(title)
            heading.setObjectName("JobTitle")
            heading.setStyleSheet("font-weight: bold; font-size: 11pt;")
            section_layout.addWidget(heading)
            
            if description:
                detail = QLabel(description)
                detail.setObjectName("Muted")
                detail.setWordWrap(True)
                section_layout.addWidget(detail)
                
            section_form = QFormLayout()
            section_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            section_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            section_form.setHorizontalSpacing(16)
            section_form.setVerticalSpacing(10)
            
            for label, widget in rows:
                section_form.addRow(label, widget)
                
            section_layout.addLayout(section_form)
            
            section_layout.addStretch()
            return section

        translation_section = make_section("Translation", (
            ("Manual literal pass", self.literal),
            ("Manual engine", self.localization),
            ("Batch engine", self.translation_engine),
            ("Fallback", self.translation_fallback),
            ("Fast workers", self.fast_workers),
            ("", self.fast_worker_hint),
        ))
        
        workspace_section = make_section(
            "Workspace",
            (
                ("Data folder", app_data_layout),
                ("Export folder", export_layout),
                ("Project import folder", project_import_layout),
                ("Manga import folder", manga_import_layout),
                ("Region shortcut", self.manual_shortcut),
                ("Title shortcut", self.title_reconstruction_shortcut),
                ("Filmstrip opening", self.filmstrip_collapse_mode),
                ("Debug artifacts", self.debug_artifacts),
                ("Support", self.diagnostics_bundle),
            ),
            "Filmstrip can use each project's saved state or start collapsed when a project is shown.",
        )
        
        region_section = make_section("Automatic Regions", (
            ("", self.translate_titles),
            ("", self.translate_sfx),
            ("", self.translate_signs),
            ("", self.translate_credits),
        ))
        
        qwen_section = make_section("Local Qwen", (
            ("Model", self.qwen_model),
            ("GGUF model", qwen_layout),
            ("Status", self.qwen_status),
            ("Download", self.qwen_estimate),
            ("", self.qwen_download),
            ("", self.qwen_test),
        ))
        
        gpu_section = make_section(
            "GPU / Native Runtime",
            (
                ("Status", self.gpu_status),
                ("Details", self.gpu_details),
                ("", self.gpu_test),
            ),
            "Hardware detection is separate from model installation. A detected GPU can still report a backend-specific dependency issue.",
        )
        
        cloud_section = make_section("Cloud Models / Keys", (
            ("OpenAI model", self.openai_model),
            ("OpenAI key", self.keys["openai"]),
            ("Compatible preset", self.openai_compatible_preset),
            ("Compatible name", self.openai_compatible_name),
            ("Compatible base URL", self.openai_compatible_base_url),
            ("Compatible model", self.openai_compatible_model),
            ("Compatible key", self.keys["openai_compatible"]),
            ("Gemini model", self.gemini_model),
            ("Gemini key", self.keys["gemini"]),
            ("Groq model", self.groq_model),
            ("Groq key", self.keys["groq"]),
            ("DeepSeek model", self.deepseek_model),
            ("DeepSeek key", self.keys["deepseek"]),
            ("Google key", self.keys["google"]),
        ))
        
        memory_section = make_section(
            "Translation Memory (TM)",
            (
                ("", self.translation_memory_enabled),
                ("", self.translation_memory_auto_learn),
                ("", self.translation_memory_store_edits),
                ("", self.translation_memory_prefer_verified),
                ("Matching", self.translation_memory_similarity),
                ("Statistics", self.translation_memory_stats),
                ("Manage", memory_actions),
            ),
            "Global exact full-segment memory shared across projects.",
        )
        
        phrase_memory_section = make_section(
            "Phrase Memory (PM v1)",
            (
                ("", self.phrase_memory_enabled),
                ("", self.phrase_memory_auto_learn),
                ("", self.phrase_memory_prefer_verified),
                ("Statistics", self.phrase_memory_stats),
                ("Manage", pm_actions),
            ),
            "Auto-learned sub-phrase constraints for terminology consistency.",
        )
        
        warning = QLabel("Cloud services are optional and may enforce quotas or charges. Automatic pages use Batch translation engine; manual text boxes use Manual translation engine.")
        warning.setWordWrap(True)
        warning.setObjectName("Muted")
        warning.setContentsMargins(24, 0, 24, 0)

        notifications_section = make_section(
            "Notifications",
            (
                ("", self.notif_enabled),
                ("", self.notif_translation_completed),
                ("", self.notif_translation_failed),
                ("", self.notif_export_completed),
                ("", self.notif_export_failed),
                ("", self.notif_review_queue),
                ("", self.notif_build_finished),
            ),
            "Notifications appear when Hydra is minimized or in the background. "
            "Error notifications always appear.",
        )

        app_details_section = make_section(
            "App Details",
            (
                ("Author", self.app_author),
                ("GitHub", self.app_github),
                ("App version", self.app_version),
                ("", self.update_actions),
                ("", self.update_status),
                ("", self.updates_check_automatically),
                ("", self.notif_updates_available),
                ("", self.updates_prompt_before_download),
            ),
        )

        sections_host = QWidget()
        sections = QHBoxLayout(sections_host)
        sections.setContentsMargins(24, 24, 24, 24)
        sections.setSpacing(16)

        left_column = QVBoxLayout()
        left_column.setSpacing(16)
        right_column = QVBoxLayout()
        right_column.setSpacing(16)

        for section in (
            translation_section,
            region_section,
            gpu_section,
            workspace_section,
            memory_section,
            notifications_section,
        ):
            left_column.addWidget(section)
        left_column.addStretch()

        for section in (
            qwen_section,
            cloud_section,
            phrase_memory_section,
            app_details_section,
        ):
            right_column.addWidget(section)
        right_column.addStretch()

        sections.addLayout(left_column, 1)
        sections.addLayout(right_column, 1)

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_scroll.setWidget(sections_host)
        
        layout.addWidget(settings_scroll, 1)
        layout.addWidget(warning)
        
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.setContentsMargins(24, 12, 24, 24)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        
        self.literal.setCurrentIndex(max(0, self.literal.findData(SETTINGS.literal_provider)))
        self.localization.setCurrentIndex(max(0, self.localization.findData(SETTINGS.localization_provider)))
        self.translation_engine.setCurrentIndex(max(0, self.translation_engine.findData(SETTINGS.translation_engine)))
        self.translation_fallback.setCurrentIndex(max(0, self.translation_fallback.findData(SETTINGS.translation_fallback_engine)))
        
        self._refresh_fast_worker_hint()
        
        self.translate_titles.setChecked(SETTINGS.translate_titles)
        self.translate_sfx.setChecked(SETTINGS.translate_sfx)
        self.translate_signs.setChecked(SETTINGS.translate_signs)
        self.translate_credits.setChecked(SETTINGS.translate_credits)
        self.debug_artifacts.setChecked(SETTINGS.debug_artifacts_enabled)

        self.notif_enabled.setChecked(SETTINGS.notif_enabled)
        self.notif_translation_completed.setChecked(SETTINGS.notif_translation_completed)
        self.notif_translation_failed.setChecked(SETTINGS.notif_translation_failed)
        self.notif_export_completed.setChecked(SETTINGS.notif_export_completed)
        self.notif_export_failed.setChecked(SETTINGS.notif_export_failed)
        self.notif_review_queue.setChecked(SETTINGS.notif_review_queue)
        self.notif_updates_available.setChecked(SETTINGS.notif_updates_available)
        self.updates_check_automatically.setChecked(SETTINGS.updates_check_automatically)
        self.updates_prompt_before_download.setChecked(SETTINGS.updates_prompt_before_download)
        self._refresh_notif_row_state(SETTINGS.notif_enabled)
        UPDATER.update_state_changed.connect(self._apply_update_state)
        self._apply_update_state(UPDATER.current_state())

        self.translation_memory_enabled.setChecked(SETTINGS.translation_memory_enabled)
        self.translation_memory_auto_learn.setChecked(SETTINGS.translation_memory_auto_learn)
        self.translation_memory_store_edits.setChecked(SETTINGS.translation_memory_store_user_edits)
        self.translation_memory_prefer_verified.setChecked(SETTINGS.translation_memory_prefer_verified)
        
        self.phrase_memory_enabled.setChecked(SETTINGS.phrase_memory_enabled)
        self.phrase_memory_auto_learn.setChecked(SETTINGS.phrase_memory_auto_learn)
        self.phrase_memory_prefer_verified.setChecked(SETTINGS.phrase_memory_prefer_verified)
        
        self._refresh_translation_memory_stats()
        
        filmstrip_index = max(0, self.filmstrip_collapse_mode.findData(SETTINGS.filmstrip_collapse_mode or "current"))
        self.filmstrip_collapse_mode.setCurrentIndex(filmstrip_index)
        
        model_index = self.qwen_model.findData(SETTINGS.qwen_model_name or "qwen3-4b")
        if model_index < 0 and SETTINGS.qwen_model_path:
            filename = Path(SETTINGS.qwen_model_path).name
            for i in range(self.qwen_model.count()):
                data = str(self.qwen_model.itemData(i) or "")
                if data == SETTINGS.qwen_model_path or filename in data:
                    model_index = i
                    break
        if model_index < 0:
            model_index = self.qwen_model.findData("custom_gguf")
        self.qwen_model.setCurrentIndex(max(0, model_index))
        self._refresh_qwen_metadata()
        if (
            SETTINGS.openai_compatible_name == "Kimi / TokenRouter"
            and SETTINGS.openai_compatible_base_url.rstrip("/") == "https://api.tokenrouter.com/v1"
            and SETTINGS.openai_compatible_model == "moonshotai/kimi-k3-free"
        ):
            self.openai_compatible_preset.setCurrentIndex(
                max(0, self.openai_compatible_preset.findData("kimi_tokenrouter"))
            )
        else:
            self.openai_compatible_preset.setCurrentIndex(
                max(0, self.openai_compatible_preset.findData("custom"))
            )

    def _apply_openai_compatible_preset(self) -> None:
        if self.openai_compatible_preset.currentData() != "kimi_tokenrouter":
            return
        self.openai_compatible_name.setText("Kimi / TokenRouter")
        self.openai_compatible_base_url.setText("https://api.tokenrouter.com/v1")
        self.openai_compatible_model.setText("moonshotai/kimi-k3-free")

    def _refresh_notif_row_state(self, enabled: bool) -> None:
        """Enable or disable per-event checkboxes based on the master toggle."""
        for cb in (
            self.notif_translation_completed,
            self.notif_translation_failed,
            self.notif_export_completed,
            self.notif_export_failed,
            self.notif_review_queue,
            self.notif_updates_available,
        ):
            cb.setEnabled(enabled)

    def _apply_update_state(self, state: UpdateState) -> None:
        self.update_check_now.setEnabled(True)
        self.update_download.setEnabled(bool(state.url))
        if state.status == STATUS_CHECKING:
            self.update_status.setText("Checking for updates...")
        elif state.status == STATUS_AVAILABLE:
            if state.dismissed:
                self.update_status.setText(
                    f"Hydra Manga TL {state.latest_version} is hidden until a newer version appears."
                )
            else:
                self.update_status.setText(
                    f"Hydra Manga TL {state.latest_version} is ready to download."
                )
        elif state.status == STATUS_UP_TO_DATE:
            self.update_status.setText(
                f"Hydra is up to date at version {state.latest_version or __version__}."
            )
        elif state.status == STATUS_FAILED:
            self.update_status.setText("Could not check for updates. Please try again.")
        else:
            self.update_status.setText("Ready to check for updates.")

    def _format_update_time(self, value: str) -> str:
        from hydra_manga_tl.core.updater import parse_utc

        parsed = parse_utc(value)
        if parsed is None:
            return "never"
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M")

    def _check_for_updates_now(self) -> None:
        self._save_update_controls()
        UPDATER.start_background_check("manual")

    def _download_update(self) -> None:
        state = UPDATER.current_state()
        if not state.url:
            QMessageBox.information(
                self,
                "Check for Updates",
                "Run Check Now first so Hydra can load the installer download link.",
            )
            return
        if self.updates_prompt_before_download.isChecked():
            answer = QMessageBox.question(
                self,
                "Download Installer?",
                (
                    f"Download Hydra Manga TL {state.latest_version or __version__}?\n\n"
                    f"File: {state.file_name}\n"
                    "The installer will open in your browser or download manager."
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        QDesktopServices.openUrl(QUrl(state.url))

    def _save_update_controls(self) -> None:
        SETTINGS.updates_check_automatically = self.updates_check_automatically.isChecked()
        SETTINGS.updates_prompt_before_download = self.updates_prompt_before_download.isChecked()
        SETTINGS.notif_updates_available = self.notif_updates_available.isChecked()
        try:
            SETTINGS.save()
        except OSError:
            pass

    def _refresh_fast_worker_hint(self) -> None:
        provider = str(self.translation_engine.currentData() or "qwen")
        profile = DEFAULT_PROVIDER_PROFILES.get(
            provider,
            DEFAULT_PROVIDER_PROFILES["marian"],
        )
        override = int(self.fast_workers.value())
        effective = resolve_provider_worker_count(profile, override)
        requested = (
            f"Auto for {profile.label}"
            if override == 0
            else f"Requested {override}"
        )
        cap = int(profile.max_parallel)
        default = int(profile.default_parallel)
        if effective < max(1, override):
            detail = f"{requested}: capped to {effective}, max {cap}."
        else:
            detail = f"{requested}: {effective} worker"
            detail += "" if effective == 1 else "s"
            detail += f", default {default}, max {cap}."
        if provider in {"qwen", "marian"}:
            detail += " Local engines run one at a time."
        elif provider == "groq":
            detail += " Groq is kept conservative for free API limits."
        elif provider == "openai_compatible":
            detail += " OpenAI-compatible routers are kept conservative by default."
        self.fast_worker_hint.setText(detail)

    def _start_gpu_probe(self, *, run_load_test: bool = False) -> None:
        if self._gpu_thread is not None and self._gpu_thread.isRunning():
            return
        self._gpu_thread = QThread(self)
        self._gpu_worker = GpuDiagnosticsWorker(run_load_test=run_load_test)
        self._gpu_worker.moveToThread(self._gpu_thread)
        self._gpu_thread.started.connect(self._gpu_worker.run)
        self._gpu_worker.completed.connect(self._gpu_diagnostics_finished)
        self._gpu_worker.completed.connect(self._gpu_thread.quit)
        self._gpu_worker.completed.connect(self._gpu_worker.deleteLater)
        self._gpu_thread.finished.connect(self._gpu_thread.deleteLater)
        self._gpu_thread.finished.connect(self._gpu_diagnostics_cleanup)
        self._gpu_thread.start()

    def _test_gpu_runtime(self) -> None:
        if self._gpu_thread is not None and self._gpu_thread.isRunning():
            return
        self.gpu_test.setEnabled(False)
        self.gpu_test.setText("Testing GPU runtime...")
        self.gpu_status.setText("Testing hardware and native backends...")
        self._start_gpu_probe(run_load_test=True)

    def _gpu_diagnostics_finished(self, report: GpuDiagnostic) -> None:
        self.gpu_status.setText(report.summary())
        self.gpu_details.setPlainText("\n".join(report.detail_lines()))
        self.gpu_test.setEnabled(True)
        self.gpu_test.setText("Test GPU runtime")

    def _gpu_diagnostics_cleanup(self) -> None:
        self._gpu_thread = None
        self._gpu_worker = None

    def _on_qwen_model_selected(self) -> None:
        key = self.qwen_model.currentData()
        if not key:
            return
        if str(key).startswith("local:"):
            for pkg in scan_local_qwen_models():
                if pkg.key == key:
                    self.qwen_model_path.setText(pkg.filename)
                    self.qwen_status.setText("Installed" if Path(pkg.filename).exists() else "Not installed")
                    self._refresh_qwen_metadata()
                    break
        elif key == "custom_gguf":
            path = self.qwen_model_path.text().strip()
            self.qwen_status.setText("Installed" if path and Path(path).exists() else "Not installed")
            self._refresh_qwen_metadata()
        else:
            package = KNOWN_MODEL_PACKAGES.get(key)
            if package:
                default_path = Path.cwd() / "models" / "qwen" / package.filename
                if default_path.exists():
                    self.qwen_model_path.setText(str(default_path))
                    self.qwen_status.setText("Installed")
                else:
                    self.qwen_status.setText("Not installed")
                self._refresh_qwen_metadata()

    def _browse_qwen_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select Qwen GGUF model", "", "GGUF models (*.gguf);;All files (*.*)")
        if path:
            self.qwen_model_path.setText(path)
            self.qwen_status.setText("Installed" if Path(path).exists() else "Not installed")
            filename = Path(path).name
            matched_index = -1
            for i in range(self.qwen_model.count()):
                data = str(self.qwen_model.itemData(i) or "")
                if data == path or data == f"local:{filename}" or filename in data:
                    matched_index = i
                    break
            if matched_index >= 0:
                self.qwen_model.setCurrentIndex(matched_index)
            else:
                custom_idx = self.qwen_model.findData("custom_gguf")
                if custom_idx >= 0:
                    self.qwen_model.setCurrentIndex(custom_idx)
            self._refresh_qwen_metadata()

    def _browse_app_data_root(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Select Hydra data folder",
            self.app_data_root.text().strip() or str(PATHS.root),
            QFileDialog.Option.ShowDirsOnly,
        )
        if path:
            self.app_data_root.setText(path)

    def _reset_app_data_root(self) -> None:
        self.app_data_root.setText(str(AppPaths.default_root().resolve()))

    def _browse_export_root(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Select export folder",
            self.export_root.text().strip()
            or str((Path.home() / "Hydra Manga TL Exports").resolve()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if path:
            self.export_root.setText(path)

    def _reset_export_root(self) -> None:
        self.export_root.setText(str((Path.home() / "Hydra Manga TL Exports").resolve()))

    def _browse_project_import_root(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Select project import folder",
            self.project_import_root.text().strip() or str(PATHS.projects),
            QFileDialog.Option.ShowDirsOnly,
        )
        if path:
            self.project_import_root.setText(path)

    def _reset_project_import_root(self) -> None:
        self.project_import_root.setText(str(PATHS.projects))

    def _browse_manga_import_root(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Select manga import folder",
            self.manga_import_root.text().strip() or str(Path.home().resolve()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if path:
            self.manga_import_root.setText(path)

    def _reset_manga_import_root(self) -> None:
        self.manga_import_root.setText(str(Path.home().resolve()))

    def _create_diagnostics_bundle(self) -> None:
        suggested = PATHS.root / "hydra-diagnostics.zip"
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Save Diagnostics Bundle",
            str(suggested),
            "ZIP archive (*.zip)",
        )
        if not selected:
            return
        try:
            project_artifacts = (
                WORKSPACE.current.artifacts
                if WORKSPACE.current is not None
                else None
            )
            destination = create_diagnostics_bundle(
                Path(selected),
                log_directory=PATHS.logs,
                settings=SETTINGS,
                project_artifacts=project_artifacts,
            )
        except (OSError, TypeError, ValueError, zipfile.BadZipFile) as error:
            QMessageBox.warning(
                self,
                "Diagnostics bundle failed",
                diagnostics_error(error),
            )
            return
        QMessageBox.information(
            self,
            "Diagnostics bundle created",
            f"Saved:\n{destination}\n\nNo credentials or project images were included.",
        )

    def _download_qwen_model(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save Qwen GGUF model", "", "GGUF models (*.gguf);;All files (*.*)")
        if path:
            self.qwen_model_path.setText(path)
            self.qwen_status.setText("Ready to download")
            self._refresh_qwen_metadata()

    def _test_qwen_translation(self) -> None:
        if self._test_thread is not None and self._test_thread.isRunning():
            return
        self.qwen_test.setEnabled(False)
        self.qwen_test.setText("Testing local engine...")
        self._test_thread = QThread(self)
        self._test_worker = TranslationTestWorker(
            qwen_model_path=self.qwen_model_path.text().strip() or None,
            preferred_engine="qwen",
            fallback_engine=None,
            qwen_model_name=self.qwen_model.currentData() or "qwen3-4b",
            provider_models={
                "groq": self.groq_model.text().strip() or SETTINGS.groq_model,
                "gemini": self.gemini_model.text().strip() or SETTINGS.gemini_model,
                "deepseek": self.deepseek_model.text().strip() or SETTINGS.deepseek_model,
                "openai": self.openai_model.text().strip() or SETTINGS.openai_model,
                "openai_compatible": (
                    self.openai_compatible_model.text().strip()
                    or SETTINGS.openai_compatible_model
                ),
            },
            provider_base_urls={
                "openai_compatible": (
                    self.openai_compatible_base_url.text().strip().rstrip("/")
                    or SETTINGS.openai_compatible_base_url
                ),
            },
        )
        self._test_worker.moveToThread(self._test_thread)
        self._test_thread.started.connect(self._test_worker.run)
        self._test_worker.completed.connect(self._translation_test_finished)
        self._test_worker.completed.connect(self._test_thread.quit)
        self._test_thread.finished.connect(self._test_worker.deleteLater)
        self._test_thread.finished.connect(self._clear_translation_test_thread)
        self._test_thread.start()

    @Slot(bool, str)
    def _translation_test_finished(self, ok: bool, message: str) -> None:
        self.qwen_test.setEnabled(True)
        self.qwen_test.setText("Test local engine")
        if ok:
            QMessageBox.information(
                self,
                "Local engine diagnostics",
                message or "The engine returned an empty translation.",
            )
        else:
            QMessageBox.warning(self, "Local engine diagnostics failed", manual_translation_error(message))

    @Slot()
    def _clear_translation_test_thread(self) -> None:
        self._test_thread = None
        self._test_worker = None

    def _refresh_qwen_metadata(self) -> None:
        key = self.qwen_model.currentData() or "qwen3-4b"
        package = KNOWN_MODEL_PACKAGES.get(key)
        if package is not None:
            self.qwen_estimate.setText(f"{package.label} · {package.quantization} · {package.estimated_download} · {package.recommended_for}")
            return
        if str(key).startswith("local:"):
            for pkg in scan_local_qwen_models():
                if pkg.key == key:
                    self.qwen_estimate.setText(f"{pkg.label} · Local GGUF file · Size: {pkg.estimated_download}")
                    return
        path_str = self.qwen_model_path.text().strip()
        if path_str and Path(path_str).is_file():
            try:
                size_gb = round(Path(path_str).stat().st_size / (1024 ** 3), 2)
                self.qwen_estimate.setText(f"Custom Model · File: {Path(path_str).name} · Size: {size_gb:.1f} GB")
            except OSError:
                self.qwen_estimate.setText(f"Custom Model · File: {Path(path_str).name}")
        else:
            self.qwen_estimate.setText("Custom GGUF model — specify file path via Browse button")

    def _open_phrase_memory_manager(self) -> None:
        dialog = PhraseMemoryManagerDialog(self)
        dialog.exec()
        self._refresh_translation_memory_stats()

    def _refresh_translation_memory_stats(self) -> None:
        stats = TRANSLATION_MEMORY.statistics()
        self.translation_memory_stats.setText(
            f"{stats.total_entries:,} entries · "
            f"{stats.verified_entries:,} verified · "
            f"{stats.exact_matches:,} exact hits · "
            f"{stats.provider_calls_saved:,} provider calls saved"
        )
        pm_stats = PHRASE_MEMORY.statistics()
        self.phrase_memory_stats.setText(
            f"{pm_stats.total_entries:,} entries · "
            f"{pm_stats.verified_entries:,} verified · "
            f"{pm_stats.total_matches:,} matches · "
            f"{pm_stats.learned_count:,} learned"
        )

    def _clear_translation_memory(self) -> None:
        answer = QMessageBox.question(
            self,
            "Clear Translation Memory",
            (
                "Delete every saved Translation Memory entry and its "
                "statistics? This cannot be undone."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        TRANSLATION_MEMORY.clear()
        self._refresh_translation_memory_stats()
        QMessageBox.information(
            self,
            "Translation Memory cleared",
            "All Translation Memory entries and statistics were removed.",
        )

    def _export_translation_memory(self) -> None:
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export Translation Memory",
            "",
            (
                "TMX (*.tmx);;Hydra JSON (*.json);;"
                "Hydra SQLite (*.db)"
            ),
        )
        if not path:
            return
        suffix = Path(path).suffix.casefold()
        if not suffix:
            suffix = (
                ".json"
                if "JSON" in selected_filter
                else ".db"
                if "SQLite" in selected_filter
                else ".tmx"
            )
            path += suffix
        try:
            destination = TRANSLATION_MEMORY.export(Path(path))
        except (OSError, TypeError, ValueError) as error:
            QMessageBox.warning(
                self,
                "Translation Memory export failed",
                memory_transfer_error(error, action="export", memory_name="Translation Memory"),
            )
            return
        QMessageBox.information(
            self,
            "Translation Memory exported",
            f"Exported Translation Memory:\n{destination}",
        )

    def _import_translation_memory(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Translation Memory",
            "",
            (
                "Translation Memory (*.tmx *.json *.db *.sqlite *.sqlite3);;"
                "All files (*.*)"
            ),
        )
        if not path:
            return
        try:
            imported = TRANSLATION_MEMORY.import_file(Path(path))
        except (OSError, TypeError, ValueError) as error:
            QMessageBox.warning(
                self,
                "Translation Memory import failed",
                memory_transfer_error(error, action="import", memory_name="Translation Memory"),
            )
            return
        self._refresh_translation_memory_stats()
        QMessageBox.information(
            self,
            "Translation Memory imported",
            f"Imported or merged {imported:,} Translation Memory entries.",
        )

    def _save(self) -> None:
        try:
            selected_data_root = Path(
                self.app_data_root.text().strip()
                or AppPaths.default_root()
            ).expanduser().resolve()
            selected_data_root.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.warning(
                self,
                "Could not use data folder",
                data_folder_error(error, "data folder"),
            )
            return
        try:
            selected_export_root = Path(
                self.export_root.text().strip()
                or (Path.home() / "Hydra Manga TL Exports")
            ).expanduser().resolve()
            selected_export_root.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.warning(
                self,
                "Could not use export folder",
                data_folder_error(error, "export folder"),
            )
            return
        old_default_import_root = PATHS.projects.resolve()
        try:
            selected_project_import_root = Path(
                self.project_import_root.text().strip() or old_default_import_root
            ).expanduser().resolve()
            selected_project_import_root.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.warning(
                self,
                "Could not use project import folder",
                data_folder_error(error, "project import folder"),
            )
            return
        try:
            selected_manga_import_root = Path(
                self.manga_import_root.text().strip() or Path.home()
            ).expanduser().resolve()
            selected_manga_import_root.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.warning(
                self,
                "Could not use manga import folder",
                data_folder_error(error, "manga import folder"),
            )
            return
        try:
            for provider, field in self.keys.items():
                if field.text().strip():
                    CREDENTIALS.set(provider, field.text())
        except RuntimeError as error:
            QMessageBox.warning(self, "Could not save API key", settings_error(error, target="API key"))
            return
            
        SETTINGS.literal_provider = self.literal.currentData()
        SETTINGS.localization_provider = self.localization.currentData()
        SETTINGS.translation_engine = self.translation_engine.currentData() or "qwen"
        SETTINGS.translation_fallback_engine = self.translation_fallback.currentData() or ""
        SETTINGS.fast_worker_override = self.fast_workers.value()
        SETTINGS.translate_titles = self.translate_titles.isChecked()
        SETTINGS.translate_sfx = self.translate_sfx.isChecked()
        SETTINGS.translate_signs = self.translate_signs.isChecked()
        SETTINGS.translate_credits = self.translate_credits.isChecked()
        SETTINGS.debug_artifacts_enabled = self.debug_artifacts.isChecked()

        SETTINGS.notif_enabled = self.notif_enabled.isChecked()
        SETTINGS.notif_translation_completed = self.notif_translation_completed.isChecked()
        SETTINGS.notif_translation_failed = self.notif_translation_failed.isChecked()
        SETTINGS.notif_export_completed = self.notif_export_completed.isChecked()
        SETTINGS.notif_export_failed = self.notif_export_failed.isChecked()
        SETTINGS.notif_review_queue = self.notif_review_queue.isChecked()
        SETTINGS.notif_updates_available = self.notif_updates_available.isChecked()
        SETTINGS.updates_check_automatically = self.updates_check_automatically.isChecked()
        SETTINGS.updates_prompt_before_download = self.updates_prompt_before_download.isChecked()
        SETTINGS.translation_memory_enabled = self.translation_memory_enabled.isChecked()
        SETTINGS.translation_memory_auto_learn = self.translation_memory_auto_learn.isChecked()
        SETTINGS.translation_memory_store_user_edits = self.translation_memory_store_edits.isChecked()
        SETTINGS.translation_memory_prefer_verified = self.translation_memory_prefer_verified.isChecked()
        
        SETTINGS.phrase_memory_enabled = self.phrase_memory_enabled.isChecked()
        SETTINGS.phrase_memory_auto_learn = self.phrase_memory_auto_learn.isChecked()
        SETTINGS.phrase_memory_prefer_verified = self.phrase_memory_prefer_verified.isChecked()
        
        SETTINGS.filmstrip_collapse_mode = self.filmstrip_collapse_mode.currentData() or "current"
        SETTINGS.qwen_model_path = self.qwen_model_path.text().strip()
        SETTINGS.qwen_model_name = self.qwen_model.currentData() or "qwen3-4b"
        SETTINGS.qwen_model_status = self.qwen_status.text().strip() or "Not installed"
        SETTINGS.gemini_model = self.gemini_model.text().strip() or "gemini-3.5-flash"
        SETTINGS.groq_model = self.groq_model.text().strip() or "openai/gpt-oss-120b"
        SETTINGS.deepseek_model = self.deepseek_model.text().strip() or "deepseek-v4-flash"
        SETTINGS.openai_model = self.openai_model.text().strip() or "gpt-4.1-mini"
        SETTINGS.openai_compatible_name = self.openai_compatible_name.text().strip() or "Kimi / TokenRouter"
        SETTINGS.openai_compatible_base_url = (
            self.openai_compatible_base_url.text().strip().rstrip("/")
            or "https://api.tokenrouter.com/v1"
        )
        SETTINGS.openai_compatible_model = (
            self.openai_compatible_model.text().strip()
            or "moonshotai/kimi-k3-free"
        )
        
        shortcut = self.manual_shortcut.keySequence().toString(QKeySequence.SequenceFormat.PortableText).strip()
        SETTINGS.manual_textbox_shortcut = shortcut or "Ctrl+D"
        title_shortcut = self.title_reconstruction_shortcut.keySequence().toString(QKeySequence.SequenceFormat.PortableText).strip()
        SETTINGS.title_reconstruction_shortcut = title_shortcut or "Ctrl+F"
        
        old_data_root = PATHS.root
        SETTINGS.app_data_root = str(selected_data_root)
        SETTINGS.export_root = str(selected_export_root)
        SETTINGS.manga_import_root = str(selected_manga_import_root)
        new_default_import_root = (selected_data_root / "projects").resolve()
        if selected_project_import_root in {
            old_default_import_root,
            new_default_import_root,
        }:
            SETTINGS.project_import_root = ""
        else:
            SETTINGS.project_import_root = str(selected_project_import_root)
        
        PATHS.configure(selected_data_root)
        PATHS.initialize()
        
        TRANSLATION_MEMORY.configure(
            PATHS.translation_memory,
            legacy_path=PATHS.legacy_translation_memory,
        )
        PHRASE_MEMORY.configure(PATHS.phrase_memory)
        SETTINGS.save()
        
        if WORKSPACE.current is not None:
            WORKSPACE.current.literal_provider = SETTINGS.literal_provider
            WORKSPACE.current.localization_provider = SETTINGS.localization_provider
            WORKSPACE.current.localization_model = SETTINGS.model_for(SETTINGS.localization_provider)
            WORKSPACE.save()
            
        if old_data_root != PATHS.root:
            QMessageBox.information(
                self,
                "Data folder updated",
                (
                    "Hydra will store new projects, logs, caches, and "
                    "Translation Memory in the selected folder. Existing "
                    "project folders are not moved automatically."
                ),
            )
        if self._stop_threads():
            self.accept()

    def _stop_threads(self) -> bool:
        stopped = True
        gpu_thread = getattr(self, "_gpu_thread", None)
        if gpu_thread is not None and gpu_thread.isRunning():
            gpu_thread.requestInterruption()
            gpu_thread.quit()
            stopped = gpu_thread.wait(5000) and stopped
        test_thread = getattr(self, "_test_thread", None)
        if test_thread is not None and test_thread.isRunning():
            test_worker = getattr(self, "_test_worker", None)
            cancel = getattr(test_worker, "cancel", None)
            if callable(cancel):
                cancel()
            test_thread.requestInterruption()
            test_thread.quit()
            stopped = test_thread.wait(5000) and stopped
        if not stopped:
            QMessageBox.information(
                self,
                "Finishing diagnostics",
                "Hydra is stopping Settings diagnostics. Please close Settings again in a moment.",
            )
        return stopped

    def closeEvent(self, event) -> None:
        if not self._stop_threads():
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self) -> None:
        if self._stop_threads():
            super().reject()


class GlossaryDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Project Glossary")
        self.setMinimumSize(480, 360)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        
        help_text = QLabel("Enter one protected name or term per line as source = English. These spellings are reused throughout this project.")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        
        self.values = QTextEdit()
        self.values.setPlaceholderText("Example:\n勇者 = Hero\n魔法 = Magic")
        
        glossary = WORKSPACE.current.glossary if WORKSPACE.current else {}
        self.values.setPlainText("\n".join(f"{source} = {target}" for source, target in glossary.items()))
        layout.addWidget(self.values)
        
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self) -> None:
        if WORKSPACE.current is None:
            self.reject()
            return
            
        glossary = {}
        for number, raw in enumerate(self.values.toPlainText().splitlines(), 1):
            if not raw.strip():
                continue
            if "=" not in raw:
                QMessageBox.warning(self, "Invalid glossary", f"Line {number} must use source = English.")
                return
                
            source, target = [value.strip() for value in raw.split("=", 1)]
            if not source or not target:
                QMessageBox.warning(self, "Invalid glossary", f"Line {number} has an empty source or translation.")
                return
                
            glossary[source] = target
            
        WORKSPACE.current.glossary = glossary
        WORKSPACE.save()
        self.accept()


class BackgroundWorkDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("BackgroundWorkDialog")
        self.setWindowTitle("Processing...")
        self.setModal(False)
        self.setFixedWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)

        heading = QLabel("Working in background")
        heading.setObjectName("WorkingTitle")
        heading.setStyleSheet("font-weight: bold; font-size: 13pt;")

        self.message = QLabel(
            "Hydra Manga TL just entered a busy mode for batch translating or manual translating, "
            "or doing big work in the background.\n\n"
            "If you close this window, your mouse cursor will show a loading spinner "
            "so it doesn't confuse you."
        )
        self.message.setWordWrap(True)
        self.message.setObjectName("Muted")

        layout.addWidget(heading)
        layout.addWidget(self.message)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(10)
        self.progress.hide()
        layout.addWidget(self.progress)

        self.closed_by_user = False

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

    def set_progress_visible(self, visible: bool) -> None:
        self.progress.setVisible(visible)

    def set_progress_fraction(self, current: int, total: int) -> None:
        total = max(1, int(total))
        current = max(0, min(int(current), total))
        self.progress.setValue(round((current / total) * 1000))

    def closeEvent(self, event):
        self.closed_by_user = True
        super().closeEvent(event)
