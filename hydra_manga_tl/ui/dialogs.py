"""Secondary dialogs for the Hydra Manga TL UI."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QThread, Signal, Slot
from PySide6.QtGui import QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog,
    QKeySequenceEdit, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget
)

from hydra_manga_tl.core.ai_bridge import HYDRA_AI
from hydra_manga_tl.core.paths import PATHS, AppPaths
from hydra_manga_tl.core.settings import CREDENTIALS, SETTINGS
from hydra_manga_tl.translation.engines.model_manager import KNOWN_MODEL_PACKAGES
from hydra_manga_tl.translation.memory import TRANSLATION_MEMORY
from hydra_manga_tl.translation.phrase_memory import PHRASE_MEMORY
from hydra_manga_tl.project.workspace import WORKSPACE



class AiCenterDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Hydra AI Dataset Dashboard"); self.resize(760, 720)
        layout = QVBoxLayout(self)
        title = QLabel("Hydra AI Center"); title.setObjectName("Heading"); layout.addWidget(title)
        self.profile_label = QLabel(); self.profile_label.setObjectName("Muted"); layout.addWidget(self.profile_label)
        intro = QLabel("Dataset readiness • Japanese → English • only explicitly approved corrections count toward training")
        intro.setWordWrap(True); intro.setObjectName("Muted"); layout.addWidget(intro)
        self.training_state = QLabel(); self.training_state.setWordWrap(True); layout.addWidget(self.training_state)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget(); self.cards_layout = QVBoxLayout(host); self.cards_layout.setContentsMargins(0, 4, 4, 4); self.cards_layout.setSpacing(8)
        self.cards = {}
        tasks = (("OCR Expert", "ocr"), ("Translation Expert", "translation"), ("Bubble Detector", "bubble"),
                 ("Layout Expert", "layout"), ("Image Cleaner", "cleaner"), ("Quality Judge", "quality"))
        for label, task in tasks:
            card = QFrame(); card.setObjectName("ProgressPanel")
            card_layout = QVBoxLayout(card); card_layout.setContentsMargins(12, 9, 12, 9); card_layout.setSpacing(5)
            heading_row = QHBoxLayout(); heading = QLabel(label); heading.setObjectName("JobTitle")
            count = QLabel("0 / 0"); count.setObjectName("Muted"); heading_row.addWidget(heading); heading_row.addStretch(); heading_row.addWidget(count)
            progress = QProgressBar(); progress.setRange(0, 1); progress.setValue(0); progress.setTextVisible(True)
            detail = QLabel("Waiting for approved corrections"); detail.setObjectName("Muted"); detail.setWordWrap(True)
            button_row = QHBoxLayout()
            dry_run = QPushButton("Dry Run"); dry_run.clicked.connect(lambda _=False, value=task: self._dry_run(value))
            button = QPushButton("Not Ready"); button.setEnabled(False); button.clicked.connect(lambda _=False, value=task: self._queue(value))
            button_row.addWidget(dry_run); button_row.addWidget(button)
            card_layout.addLayout(heading_row); card_layout.addWidget(progress); card_layout.addWidget(detail); card_layout.addLayout(button_row)
            self.cards_layout.addWidget(card); self.cards[task] = {"count": count, "progress": progress, "detail": detail, "button": button, "dry_run": dry_run}
        self.cards_layout.addStretch(); scroll.setWidget(host); layout.addWidget(scroll, 1)
        controls = QHBoxLayout()
        refresh = QPushButton("Refresh"); refresh.clicked.connect(self.refresh)
        pause = QPushButton("Pause Training"); pause.clicked.connect(self._pause)
        resume = QPushButton("Resume Training"); resume.clicked.connect(self._resume)
        close = QPushButton("Close"); close.clicked.connect(self.accept)
        controls.addWidget(refresh); controls.addWidget(pause); controls.addWidget(resume); controls.addStretch(); controls.addWidget(close)
        layout.addLayout(controls); self.refresh()

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
            approved = int(item.get("approved", 0)); required = max(1, int(item.get("required", 1)))
            unit = item.get("unit", "samples")
            widgets["count"].setText(f"{approved:,} / {required:,} {unit}")
            widgets["progress"].setRange(0, required); widgets["progress"].setValue(min(approved, required))
            widgets["progress"].setFormat(f"%p%  •  %v / {required:,}")
            golden = int(item.get("golden", 0)); required_golden = int(item.get("required_golden", 0))
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
            widgets["button"].setEnabled(ready); widgets["button"].setText("Queue Training" if ready else "Not Ready")

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
        HYDRA_AI.pause_training(); self.refresh()

    def _resume(self) -> None:
        HYDRA_AI.resume_training(); self.refresh()

class WorkingDialog(QDialog):
    def __init__(self, title: str, message: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("WorkingDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setFixedWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        heading = QLabel(title)
        heading.setObjectName("WorkingTitle")
        self.message = QLabel(message)
        self.message.setObjectName("Muted")
        self.message.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.log = QTextEdit()
        self.log.setObjectName("WorkingLog")
        self.log.setReadOnly(True)
        self.log.setFixedHeight(120)
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
        self.setFixedWidth(380)

        self.output_type = QComboBox()
        self.output_type.addItem("Image folder", "folder")
        self.output_type.addItem("ZIP archive", "zip")
        self.output_type.addItem("CBZ comic archive", "cbz")

        self.image_format = QComboBox()
        self.image_format.addItem("PNG", "png")
        self.image_format.addItem("JPEG", "jpg")
        self.image_format.addItem("WebP", "webp")

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("Output", self.output_type)
        form.addRow("Image format", self.image_format)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Export")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
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
        layout.setContentsMargins(14, 14, 14, 14)
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
        available = self.preview.size() - QSize(8, 8)
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
    ) -> None:
        super().__init__()
        self.qwen_model_path = qwen_model_path
        self.preferred_engine = preferred_engine
        self.fallback_engine = fallback_engine
        self.qwen_model_name = qwen_model_name
        self.provider_models = provider_models

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
            allow_local_fallback_for_cloud=True,
            translation_memory_enabled=False,
        )
        page = PageDialogue(
            source_language="Japanese",
            target_language="en",
            dialogue=[{"id": "r1", "text": "待て！"}],
        )
        try:
            manager.load()
            result = manager.translate_page(page)
            sample = str(result.translations[0].get("text", "")) if result.translations else ""
            self.completed.emit(True, sample)
        except Exception as error:
            self.completed.emit(False, str(error) or type(error).__name__)
        finally:
            manager.unload()


class PhraseMemoryManagerDialog(QDialog):
    """Phrase Memory (PM v1) Manager dialog for viewing, editing, verifying, deleting, importing, and exporting entries."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Phrase Memory Manager (PM v1)")
        self.resize(880, 580)
        layout = QVBoxLayout(self)

        header = QLabel("Phrase Memory (PM v1)")
        header.setObjectName("Heading")
        layout.addWidget(header)

        sub = QLabel("Deterministic sub-phrase memory learned from validated translations. Entries supply terminology hints to translation providers.")
        sub.setWordWrap(True)
        sub.setObjectName("Muted")
        layout.addWidget(sub)

        filter_layout = QHBoxLayout()
        filter_label = QLabel("Search:")
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter source or target phrase...")
        self.search_input.textChanged.connect(self._apply_filter)
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
        layout.addWidget(self.table, 1)

        self.stats_label = QLabel()
        self.stats_label.setObjectName("Muted")
        layout.addWidget(self.stats_label)

        btn_layout = QHBoxLayout()
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
            QMessageBox.warning(self, "Import Failed", str(error))

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
            QMessageBox.warning(self, "Export Failed", str(error))

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


class SettingsDialog(QDialog):

    """Local-first provider preferences with secrets stored outside settings JSON."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Hydra Settings")
        self.setMinimumWidth(760)
        layout = QVBoxLayout(self)
        self.literal = QComboBox(); self.literal.addItem("MarianMT (Local)", "marian"); self.literal.addItem("Google Cloud Translation", "google")
        self.localization = QComboBox()
        for label, value in (("Local manga cleanup", "local"), ("Gemini", "gemini"), ("Groq", "groq"), ("DeepSeek", "deepseek")):
            self.localization.addItem(label, value)
        self.translation_engine = QComboBox()
        self.translation_engine.addItem("Groq", "groq")
        self.translation_engine.addItem("Google Translate", "google")
        self.translation_engine.addItem("Gemini", "gemini")
        self.translation_engine.addItem("Marian fallback", "marian")
        self.translation_engine.addItem("Local Qwen (optional)", "qwen")
        self.translation_fallback = QComboBox()
        self.translation_fallback.addItem("No automatic fallback", "")
        self.translation_fallback.addItem("Marian (local)", "marian")
        self.translation_fallback.addItem("Groq", "groq")
        self.translation_fallback.addItem("Google Translate", "google")
        self.translation_fallback.addItem("Gemini", "gemini")
        self.fast_workers = QSpinBox()
        self.fast_workers.setRange(0, 6)
        self.fast_workers.setSpecialValueText("Auto")
        self.fast_workers.setValue(max(0, min(6, int(SETTINGS.fast_worker_override))))
        self.fast_workers.setToolTip("Fast mode page workers. Auto uses 2, 4, or 6 based on logical CPU threads.")
        self.translate_titles = QCheckBox("Translate titles automatically")
        self.translate_sfx = QCheckBox("Translate SFX automatically")
        self.translate_signs = QCheckBox("Translate signs automatically")
        self.translate_credits = QCheckBox("Translate credits automatically")
        self.translation_memory_enabled = QCheckBox(
            "Enable Translation Memory"
        )
        self.translation_memory_auto_learn = QCheckBox(
            "Automatically learn validated translations"
        )
        self.translation_memory_store_edits = QCheckBox(
            "Store user translation edits as verified"
        )
        self.translation_memory_prefer_verified = QCheckBox(
            "Prefer verified entries"
        )
        self.translation_memory_similarity = QLabel("Exact only (100%)")
        self.translation_memory_stats = QLabel()
        self.translation_memory_stats.setObjectName("Muted")
        self.translation_memory_stats.setWordWrap(True)
        self.translation_memory_import = QPushButton("Import")
        self.translation_memory_import.clicked.connect(
            self._import_translation_memory
        )
        self.translation_memory_export = QPushButton("Export")
        self.translation_memory_export.clicked.connect(
            self._export_translation_memory
        )
        self.translation_memory_clear = QPushButton("Clear Memory")
        self.translation_memory_clear.clicked.connect(
            self._clear_translation_memory
        )
        memory_actions = QWidget()
        memory_actions_layout = QHBoxLayout(memory_actions)
        memory_actions_layout.setContentsMargins(0, 0, 0, 0)
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
        self.qwen_model = QComboBox()
        for package in KNOWN_MODEL_PACKAGES.values():
            self.qwen_model.addItem(package.label, package.key)
        self.qwen_model_path = QLineEdit(SETTINGS.qwen_model_path)
        self.qwen_model_path.setPlaceholderText("Path to a .gguf model")
        self.qwen_status = QLabel(SETTINGS.qwen_model_status or "Not installed")
        self.qwen_estimate = QLabel("Estimated download: not available")
        self.qwen_estimate.setWordWrap(True)
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
        app_data_layout = QHBoxLayout()
        app_data_layout.addWidget(self.app_data_root, 1)
        app_data_layout.addWidget(self.app_data_browse)
        app_data_layout.addWidget(self.app_data_default)
        self.keys = {}
        for provider in ("google", "gemini", "groq", "deepseek"):
            field = QLineEdit(); field.setEchoMode(QLineEdit.EchoMode.Password)
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
        def make_section(title: str, rows: tuple[tuple[str, object], ...], description: str = "") -> QFrame:
            section = QFrame()
            section.setObjectName("ProgressPanel")
            section_layout = QVBoxLayout(section)
            section_layout.setContentsMargins(10, 8, 10, 8)
            section_layout.setSpacing(6)
            heading = QLabel(title)
            heading.setObjectName("JobTitle")
            section_layout.addWidget(heading)
            if description:
                detail = QLabel(description)
                detail.setObjectName("Muted")
                detail.setWordWrap(True)
                section_layout.addWidget(detail)
            section_form = QFormLayout()
            section_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            section_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            section_form.setHorizontalSpacing(8)
            section_form.setVerticalSpacing(6)
            for label, widget in rows:
                section_form.addRow(label, widget)
            section_layout.addLayout(section_form)
            return section

        translation_section = make_section("Translation", (
            ("Manual literal pass", self.literal),
            ("Manual engine", self.localization),
            ("Batch engine", self.translation_engine),
            ("Fallback", self.translation_fallback),
            ("Fast workers", self.fast_workers),
        ))
        workspace_section = make_section(
            "Workspace",
            (
                ("Data folder", app_data_layout),
                ("Region shortcut", self.manual_shortcut),
                ("Title shortcut", self.title_reconstruction_shortcut),
                ("Filmstrip opening", self.filmstrip_collapse_mode),
                ("Debug artifacts", self.debug_artifacts),
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
        cloud_section = make_section("Cloud Models / Keys", (
            ("Gemini model", self.gemini_model),
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
            (
                "Global exact full-segment memory shared across projects."
            ),
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
            (
                "Auto-learned sub-phrase constraints for terminology consistency."
            ),
        )
        warning = QLabel("Cloud services are optional and may enforce quotas or charges. Automatic pages use Batch translation engine; manual text boxes use Manual translation engine.")
        warning.setWordWrap(True); warning.setObjectName("Muted")
        sections = QGridLayout()
        sections.setHorizontalSpacing(10)
        sections.setVerticalSpacing(10)
        sections.addWidget(translation_section, 0, 0)
        sections.addWidget(qwen_section, 0, 1, 2, 1)
        sections.addWidget(region_section, 1, 0)
        sections.addWidget(workspace_section, 2, 0)
        sections.addWidget(cloud_section, 2, 1)
        sections.addWidget(memory_section, 3, 0)
        sections.addWidget(phrase_memory_section, 3, 1)
        sections_host = QWidget()
        sections_host.setLayout(sections)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_scroll.setWidget(sections_host)
        layout.addWidget(settings_scroll, 1)
        layout.addWidget(warning)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.resize(1040, 760)
        self.literal.setCurrentIndex(max(0, self.literal.findData(SETTINGS.literal_provider)))
        self.localization.setCurrentIndex(max(0, self.localization.findData(SETTINGS.localization_provider)))
        self.translation_engine.setCurrentIndex(max(0, self.translation_engine.findData(SETTINGS.translation_engine)))
        self.translation_fallback.setCurrentIndex(max(0, self.translation_fallback.findData(SETTINGS.translation_fallback_engine)))
        self.translate_titles.setChecked(SETTINGS.translate_titles)
        self.translate_sfx.setChecked(SETTINGS.translate_sfx)
        self.translate_signs.setChecked(SETTINGS.translate_signs)
        self.translate_credits.setChecked(SETTINGS.translate_credits)
        self.debug_artifacts.setChecked(SETTINGS.debug_artifacts_enabled)
        self.translation_memory_enabled.setChecked(
            SETTINGS.translation_memory_enabled
        )
        self.translation_memory_auto_learn.setChecked(
            SETTINGS.translation_memory_auto_learn
        )
        self.translation_memory_store_edits.setChecked(
            SETTINGS.translation_memory_store_user_edits
        )
        self.translation_memory_prefer_verified.setChecked(
            SETTINGS.translation_memory_prefer_verified
        )
        self.phrase_memory_enabled.setChecked(
            SETTINGS.phrase_memory_enabled
        )
        self.phrase_memory_auto_learn.setChecked(
            SETTINGS.phrase_memory_auto_learn
        )
        self.phrase_memory_prefer_verified.setChecked(
            SETTINGS.phrase_memory_prefer_verified
        )
        self._refresh_translation_memory_stats()
        filmstrip_index = max(0, self.filmstrip_collapse_mode.findData(SETTINGS.filmstrip_collapse_mode or "current"))
        self.filmstrip_collapse_mode.setCurrentIndex(filmstrip_index)
        model_index = max(0, self.qwen_model.findData(SETTINGS.qwen_model_name or "qwen3-4b"))
        self.qwen_model.setCurrentIndex(model_index)
        self._refresh_qwen_metadata()


    def _browse_qwen_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select Qwen GGUF model", "", "GGUF models (*.gguf);;All files (*.*)")
        if path:
            self.qwen_model_path.setText(path)
            self.qwen_status.setText("Installed" if Path(path).exists() else "Not installed")
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

    def _download_qwen_model(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save Qwen GGUF model", "", "GGUF models (*.gguf);;All files (*.*)")
        if path:
            self.qwen_model_path.setText(path)
            self.qwen_status.setText("Ready to download")
            self._refresh_qwen_metadata()

    def _test_qwen_translation(self) -> None:
        if getattr(self, "_test_thread", None) is not None:
            return
        self.qwen_test.setEnabled(False)
        self.qwen_test.setText("Testing...")
        self._test_thread = QThread(self)
        self._test_worker = TranslationTestWorker(
            qwen_model_path=self.qwen_model_path.text().strip() or None,
            preferred_engine=self.translation_engine.currentData() or "qwen",
            fallback_engine=self.translation_fallback.currentData() or None,
            qwen_model_name=self.qwen_model.currentData() or "qwen3-4b",
            provider_models={
                "groq": self.groq_model.text().strip() or SETTINGS.groq_model,
                "gemini": self.gemini_model.text().strip() or SETTINGS.gemini_model,
                "deepseek": self.deepseek_model.text().strip() or SETTINGS.deepseek_model,
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
        self.qwen_test.setText("Test translation")
        if ok:
            QMessageBox.information(self, "Test translation", message or "The engine returned an empty translation.")
        else:
            QMessageBox.warning(self, "Test translation failed", message)

    @Slot()
    def _clear_translation_test_thread(self) -> None:
        self._test_thread = None
        self._test_worker = None

    def _refresh_qwen_metadata(self) -> None:
        package = KNOWN_MODEL_PACKAGES.get(self.qwen_model.currentData() or "qwen3-4b")
        if package is None:
            self.qwen_estimate.setText("Estimated download: not available")
            return
        self.qwen_estimate.setText(f"{package.label} · {package.quantization} · {package.estimated_download} · {package.recommended_for}")

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
                str(error),
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
                str(error),
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
                str(error),
            )
            return
        try:
            for provider, field in self.keys.items():
                if field.text().strip():
                    CREDENTIALS.set(provider, field.text())
        except RuntimeError as error:
            QMessageBox.warning(self, "Could not save API key", str(error)); return
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
        SETTINGS.translation_memory_enabled = (
            self.translation_memory_enabled.isChecked()
        )
        SETTINGS.translation_memory_auto_learn = (
            self.translation_memory_auto_learn.isChecked()
        )
        SETTINGS.translation_memory_store_user_edits = (
            self.translation_memory_store_edits.isChecked()
        )
        SETTINGS.translation_memory_prefer_verified = (
            self.translation_memory_prefer_verified.isChecked()
        )
        SETTINGS.phrase_memory_enabled = (
            self.phrase_memory_enabled.isChecked()
        )
        SETTINGS.phrase_memory_auto_learn = (
            self.phrase_memory_auto_learn.isChecked()
        )
        SETTINGS.phrase_memory_prefer_verified = (
            self.phrase_memory_prefer_verified.isChecked()
        )
        SETTINGS.filmstrip_collapse_mode = self.filmstrip_collapse_mode.currentData() or "current"
        SETTINGS.qwen_model_path = self.qwen_model_path.text().strip()
        SETTINGS.qwen_model_name = self.qwen_model.currentData() or "qwen3-4b"
        SETTINGS.qwen_model_status = self.qwen_status.text().strip() or "Not installed"
        SETTINGS.gemini_model = self.gemini_model.text().strip() or "gemini-3.5-flash"
        SETTINGS.groq_model = self.groq_model.text().strip() or "openai/gpt-oss-120b"
        SETTINGS.deepseek_model = self.deepseek_model.text().strip() or "deepseek-v4-flash"
        shortcut = self.manual_shortcut.keySequence().toString(QKeySequence.SequenceFormat.PortableText).strip()
        SETTINGS.manual_textbox_shortcut = shortcut or "Ctrl+D"
        title_shortcut = self.title_reconstruction_shortcut.keySequence().toString(QKeySequence.SequenceFormat.PortableText).strip()
        SETTINGS.title_reconstruction_shortcut = title_shortcut or "Ctrl+F"
        old_data_root = PATHS.root
        SETTINGS.app_data_root = str(selected_data_root)
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
