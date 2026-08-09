"""Settings dialog action handlers."""

from __future__ import annotations

import sys

from .common import *  # noqa: F401,F403
from .gpu import GpuDiagnosticsWorker
from .phrase_memory import PhraseMemoryManagerDialog
from .translation_test import TranslationTestWorker


def _translation_memory():
    package = sys.modules.get(__package__)
    return getattr(package, "TRANSLATION_MEMORY", TRANSLATION_MEMORY)


class SettingsActionsMixin:
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
        stats = _translation_memory().statistics()
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
        _translation_memory().clear()
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
            destination = _translation_memory().export(Path(path))
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
            imported = _translation_memory().import_file(Path(path))
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
        
        _translation_memory().configure(
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
