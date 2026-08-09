"""SettingsDialog implementation."""

from __future__ import annotations

from .common import *  # noqa: F401,F403
from .common import __version__, _add_provider_item
from .settings_actions import SettingsActionsMixin


class SettingsDialog(SettingsActionsMixin, QDialog):
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
        self.app_website = QLabel(
            f'<a href="{APP_WEBSITE_URL}">{APP_WEBSITE_URL}</a>'
        )
        self.app_website.setObjectName("Muted")
        self.app_website.setOpenExternalLinks(True)
        self.app_website.setTextInteractionFlags(
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
                ("Website", self.app_website),
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
