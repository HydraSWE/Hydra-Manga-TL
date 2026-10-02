"""Settings dialog action handlers."""

from __future__ import annotations

import sys
import threading

from .common import *  # noqa: F401,F403
from .gpu import GpuDiagnosticsWorker
from .phrase_memory import PhraseMemoryManagerDialog
from .translation_test import TranslationTestWorker
from hydra_manga_tl.translation.engines.model_download import (
    ModelDownloadCancelled,
    download_model_file,
    partial_model_download_size,
    remove_partial_model_download,
)


def _translation_memory():
    package = sys.modules.get(__package__)
    return getattr(package, "TRANSLATION_MEMORY", TRANSLATION_MEMORY)


class QwenModelDownloadWorker(QObject):
    progress = Signal(object, object)
    completed = Signal(bool, str, str)
    finished = Signal()

    def __init__(self, package: ModelPackage, destination: Path) -> None:
        super().__init__()
        self._package = package
        self._destination = destination
        self._cancelled = threading.Event()
        self._remove_partial = False

    @Slot()
    def run(self) -> None:
        try:
            path = download_model_file(
                url=self._package.download_url,
                destination=self._destination,
                min_size_bytes=self._package.min_download_size_bytes,
                progress=self.progress.emit,
                cancel_requested=self._cancelled.is_set,
                remove_partial_on_cancel=self._remove_partial,
            )
        except ModelDownloadCancelled:
            if self._remove_partial:
                self.completed.emit(False, "", "Model download was cancelled.")
            else:
                self.completed.emit(False, "", "Model download was paused.")
        except Exception as error:
            self.completed.emit(False, "", str(error) or "Model download failed.")
        else:
            self.completed.emit(True, str(path), "")
        finally:
            self.finished.emit()

    def cancel(self, *, remove_partial: bool = False) -> None:
        self._remove_partial = bool(remove_partial)
        self._cancelled.set()


class SettingsActionsMixin:
    def _deferred_settings_refresh(self) -> None:
        if not self.isVisible():
            return
        self._load_local_qwen_models()
        self._refresh_qwen_metadata()
        self._refresh_translation_memory_stats()

    def _load_local_qwen_models(self) -> None:
        if getattr(self, "_local_qwen_models_loaded", False):
            return
        self._local_qwen_models_loaded = True
        self._local_qwen_models = list(scan_local_qwen_models())
        existing = {
            str(self.qwen_model.itemData(index) or "")
            for index in range(self.qwen_model.count())
        }
        custom_index = self.qwen_model.findData("custom_gguf")
        for local_pkg in self._local_qwen_models:
            if local_pkg.key in existing:
                continue
            insert_at = custom_index if custom_index >= 0 else self.qwen_model.count()
            self.qwen_model.insertItem(insert_at, local_pkg.label, local_pkg.key)
            if custom_index >= 0:
                custom_index += 1
            existing.add(local_pkg.key)
        if SETTINGS.qwen_model_path:
            filename = Path(SETTINGS.qwen_model_path).name
            for index in range(self.qwen_model.count()):
                data = str(self.qwen_model.itemData(index) or "")
                if data == SETTINGS.qwen_model_path or filename in data:
                    self.qwen_model.setCurrentIndex(index)
                    break
        self._refresh_qwen_download_state()

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
            packages = list(getattr(self, "_local_qwen_models", [])) or scan_local_qwen_models()
            for pkg in packages:
                if pkg.key == key:
                    self.qwen_model_path.setText(pkg.filename)
                    self.qwen_status.setText("Installed" if Path(pkg.filename).exists() else "Not installed")
                    self._refresh_qwen_metadata()
                    self._refresh_qwen_download_state()
                    break
        elif key == "custom_gguf":
            path = self.qwen_model_path.text().strip()
            self.qwen_status.setText("Installed" if path and Path(path).exists() else "Not installed")
            self._refresh_qwen_metadata()
            self._refresh_qwen_download_state()
        else:
            package = KNOWN_MODEL_PACKAGES.get(key)
            if package:
                default_path = Path.cwd() / "models" / "qwen" / package.target_filename
                self.qwen_model_path.setText(str(default_path))
                if default_path.exists():
                    self.qwen_status.setText("Installed")
                else:
                    self.qwen_status.setText("Not installed")
                self._refresh_qwen_metadata()
                self._refresh_qwen_download_state()

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
            self._refresh_qwen_download_state()

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
        if self._qwen_download_thread is not None and self._qwen_download_thread.isRunning():
            return
        package = self._selected_downloadable_qwen_package()
        if package is None:
            self._refresh_qwen_download_state()
            return
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose Qwen model download folder",
            str(Path.cwd() / "models" / "qwen"),
        )
        if not folder:
            return
        destination = Path(folder) / package.target_filename
        self._qwen_previous_path = self.qwen_model_path.text().strip()
        self._qwen_download_destination = str(destination)
        if destination.exists():
            try:
                from hydra_manga_tl.translation.engines.model_download import validate_gguf_model_file

                validate_gguf_model_file(
                    destination,
                    min_size_bytes=package.min_download_size_bytes,
                )
            except Exception as error:
                QMessageBox.warning(
                    self,
                    "Model download failed",
                    str(error) or "The existing model file could not be used.",
                )
                self._refresh_qwen_download_state()
                return
            self.qwen_model_path.setText(str(destination))
            self.qwen_status.setText("Installed")
            self._refresh_qwen_metadata()
            self._refresh_qwen_download_state()
            return

        self.qwen_model_path.setText(str(destination))
        self.qwen_status.setText("Downloading...")
        self.qwen_progress.setValue(0)
        self.qwen_progress.setVisible(True)
        self.qwen_progress.setFormat("%p%")
        self._set_qwen_download_controls_enabled(False)

        self._qwen_download_thread = QThread(self)
        self._qwen_download_worker = QwenModelDownloadWorker(package, destination)
        self._qwen_download_worker.moveToThread(self._qwen_download_thread)
        self._qwen_download_thread.started.connect(self._qwen_download_worker.run)
        self._qwen_download_worker.progress.connect(self.qwen_download_progress_changed.emit)
        self._qwen_download_worker.completed.connect(self.qwen_download_completed.emit)
        self._qwen_download_worker.finished.connect(self._qwen_download_thread.quit)
        self._qwen_download_worker.finished.connect(self._qwen_download_worker.deleteLater)
        self._qwen_download_thread.finished.connect(self._clear_qwen_download_thread)
        self._qwen_download_thread.start()
        self._refresh_qwen_download_state()

    def _pause_qwen_download(self) -> None:
        worker = getattr(self, "_qwen_download_worker", None)
        cancel = getattr(worker, "cancel", None)
        if callable(cancel):
            cancel(remove_partial=False)
        self.qwen_status.setText("Pausing download...")

    def _cancel_qwen_download(self) -> None:
        worker = getattr(self, "_qwen_download_worker", None)
        cancel = getattr(worker, "cancel", None)
        if callable(cancel):
            cancel(remove_partial=False)
            self.qwen_status.setText("Stopping download...")
            return
        path = self.qwen_model_path.text().strip()
        if path:
            remove_partial_model_download(path)
        self.qwen_status.setText("Download cancelled")
        self._refresh_qwen_download_state()

    @Slot(object, object)
    def _qwen_download_progress(self, bytes_done: object, total_bytes: object) -> None:
        try:
            done = int(bytes_done)
        except (TypeError, ValueError, OverflowError):
            done = 0
        try:
            total = int(total_bytes)
        except (TypeError, ValueError, OverflowError):
            total = 0
        if total > 0:
            percent = max(0, min(100, int(done * 100 / total)))
            self.qwen_progress.setRange(0, 100)
            self.qwen_progress.setValue(percent)
            self.qwen_progress.setFormat(f"{percent}%")
        else:
            self.qwen_progress.setRange(0, 0)
            self.qwen_progress.setFormat("Downloading...")
        downloaded_gb = done / (1024 ** 3)
        if total > 0:
            total_gb = total / (1024 ** 3)
            self.qwen_status.setText(f"Downloading model... {downloaded_gb:.1f}/{total_gb:.1f} GB")
        else:
            self.qwen_status.setText(f"Downloading model... {downloaded_gb:.1f} GB")

    @Slot(bool, str, str)
    def _qwen_download_finished(self, ok: bool, path: str, message: str) -> None:
        self.qwen_progress.setVisible(False)
        self.qwen_progress.setRange(0, 100)
        self._set_qwen_download_controls_enabled(True)
        self._qwen_download_thread = None
        self._qwen_download_worker = None
        if ok:
            self.qwen_model_path.setText(path)
            self.qwen_status.setText("Installed")
            self._refresh_qwen_metadata()
            self._refresh_qwen_download_state()
            return
        previous_path = getattr(self, "_qwen_previous_path", "")
        self.qwen_model_path.setText(previous_path)
        current_destination = getattr(self, "_qwen_download_destination", "")
        partial_size = partial_model_download_size(current_destination) if current_destination else 0
        if "paused" in message.lower() and partial_size > 0:
            self.qwen_model_path.setText(current_destination)
            self.qwen_status.setText("Paused - ready to resume")
        elif "cancelled" in message.lower():
            self.qwen_status.setText("Download cancelled")
        else:
            self.qwen_status.setText(
                "Installed" if previous_path and Path(previous_path).is_file() else "Download incomplete"
            )
        self._refresh_qwen_download_state()
        if "paused" in message.lower() or "cancelled" in message.lower():
            return
        QMessageBox.warning(
            self,
            "Model download failed",
            message or "Hydra could not download the selected Qwen model.",
        )

    @Slot()
    def _clear_qwen_download_thread(self) -> None:
        sender = self.sender()
        if sender is None or sender is self._qwen_download_thread:
            self._qwen_download_thread = None
            self._qwen_download_worker = None

    def _test_qwen_translation(self) -> None:
        if self._test_thread is not None and self._test_thread.isRunning():
            return
        from hydra_manga_tl.translation.runtime import (
            TranslationRuntimeConfig,
            active_warmup_matches,
        )

        runtime_config = TranslationRuntimeConfig(
            preferred_engine="qwen",
            fallback_engine="",
            qwen_model_path=self.qwen_model_path.text().strip(),
            qwen_model_name=str(self.qwen_model.currentData() or "qwen3-4b"),
            provider_models=tuple(sorted((
                ("groq", self.groq_model.text().strip() or SETTINGS.groq_model),
                ("gemini", self.gemini_model.text().strip() or SETTINGS.gemini_model),
                ("deepseek", self.deepseek_model.text().strip() or SETTINGS.deepseek_model),
                ("openai", self.openai_model.text().strip() or SETTINGS.openai_model),
                (
                    "openai_compatible",
                    self.openai_compatible_model.text().strip()
                    or SETTINGS.openai_compatible_model,
                ),
            ))),
            provider_base_urls=tuple(sorted((
                (
                    "openai_compatible",
                    self.openai_compatible_base_url.text().strip().rstrip("/")
                    or SETTINGS.openai_compatible_base_url,
                ),
            ))),
            allow_local_fallback_for_cloud=True,
            translation_memory_enabled=False,
        )
        startup_config = TranslationRuntimeConfig.from_mapping({
            "translation_engine": "qwen",
            "translation_fallback_engine": SETTINGS.translation_fallback_engine,
            "qwen_model_path": self.qwen_model_path.text().strip(),
            "qwen_model_name": self.qwen_model.currentData() or "qwen3-4b",
        })
        if active_warmup_matches(runtime_config) or active_warmup_matches(startup_config):
            self.qwen_status.setText("Local engine is still warming up")
            self.qwen_test.setEnabled(False)
            self.qwen_test.setText("Warmup in progress...")
            QTimer.singleShot(1200, self._refresh_qwen_test_after_warmup)
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
        self._test_worker.completed.connect(self.translation_test_completed.emit)
        self._test_worker.finished.connect(self._test_thread.quit)
        self._test_worker.finished.connect(self._test_worker.deleteLater)
        self._test_thread.finished.connect(self._clear_translation_test_thread)
        self._test_thread.start()

    def _refresh_qwen_test_after_warmup(self) -> None:
        from hydra_manga_tl.translation.runtime import (
            TranslationRuntimeConfig,
            active_warmup_matches,
            current_warmup_state,
        )

        runtime_config = TranslationRuntimeConfig.from_mapping({
            "translation_engine": "qwen",
            "translation_fallback_engine": SETTINGS.translation_fallback_engine,
            "qwen_model_path": self.qwen_model_path.text().strip(),
            "qwen_model_name": self.qwen_model.currentData() or "qwen3-4b",
        })
        if active_warmup_matches(runtime_config):
            QTimer.singleShot(1200, self._refresh_qwen_test_after_warmup)
            return
        state = current_warmup_state(runtime_config)
        if state.state == "ready":
            self.qwen_status.setText("Installed")
        elif state.state == "failed":
            self.qwen_status.setText("Warmup failed")
        elif state.state == "skipped":
            self.qwen_status.setText("Not installed")
        self.qwen_test.setEnabled(True)
        self.qwen_test.setText("Test local engine")

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
            QMessageBox.warning(
                self,
                "Local engine diagnostics failed",
                message or "Local engine diagnostics did not return details.",
            )

    @Slot()
    def _clear_translation_test_thread(self) -> None:
        self._test_thread = None
        self._test_worker = None

    def _refresh_qwen_metadata(self) -> None:
        key = self.qwen_model.currentData() or "qwen3-4b"
        package = KNOWN_MODEL_PACKAGES.get(key)
        if package is not None:
            self.qwen_estimate.setText(f"{package.label} · {package.quantization} · {package.estimated_download} · {package.recommended_for}")
            self._refresh_qwen_download_state()
            return
        if str(key).startswith("local:"):
            for pkg in getattr(self, "_local_qwen_models", []):
                if pkg.key == key:
                    self.qwen_estimate.setText(f"{pkg.label} · Local GGUF file · Size: {pkg.estimated_download}")
                    self._refresh_qwen_download_state()
                    return
            self.qwen_estimate.setText("Local GGUF model")
            self._refresh_qwen_download_state()
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
        self._refresh_qwen_download_state()

    def _selected_downloadable_qwen_package(self) -> ModelPackage | None:
        key = str(self.qwen_model.currentData() or "")
        package = KNOWN_MODEL_PACKAGES.get(key)
        if package is None or not package.download_url:
            return None
        return package

    def _refresh_qwen_download_state(self) -> None:
        if not hasattr(self, "qwen_download"):
            return
        running = (
            self._qwen_download_thread is not None
            and self._qwen_download_thread.isRunning()
        )
        if running:
            self._set_qwen_row_visible("download", True)
            self._set_qwen_row_visible("progress", True)
            self.qwen_download.setVisible(False)
            self.qwen_download.setText("Download Model")
            self.qwen_pause.setVisible(True)
            self.qwen_cancel.setVisible(True)
            self.qwen_progress.setVisible(True)
            self.qwen_test.setVisible(False)
            return
        self.qwen_pause.setVisible(False)
        self.qwen_cancel.setVisible(False)
        self.qwen_test.setVisible(True)
        package = self._selected_downloadable_qwen_package()
        if package is None:
            self._set_qwen_row_visible("download", False)
            self._set_qwen_row_visible("progress", False)
            path = Path(self.qwen_model_path.text().strip() or "")
            self.qwen_test.setVisible(path.is_file())
            return
        path = Path(self.qwen_model_path.text().strip() or "")
        installed = path.is_file()
        if installed:
            self.qwen_status.setText("Installed")
            self._set_qwen_row_visible("download", False)
            self._set_qwen_row_visible("progress", False)
            self.qwen_test.setVisible(True)
            return
        partial_size = 0 if installed else partial_model_download_size(path)
        if partial_size > 0:
            self._set_qwen_row_visible("download", True)
            self._set_qwen_row_visible("progress", True)
            self.qwen_download.setText("Resume Download")
            self.qwen_status.setText("Paused - ready to resume")
            self.qwen_progress.setVisible(True)
            self.qwen_progress.setRange(0, 100)
            self.qwen_progress.setValue(0)
            self.qwen_progress.setFormat(f"Partial: {partial_size / (1024 ** 3):.1f} GB")
            self.qwen_cancel.setVisible(True)
            self.qwen_test.setVisible(False)
        else:
            self._set_qwen_row_visible("download", True)
            self._set_qwen_row_visible("progress", False)
            self.qwen_download.setText("Download Model")
            self.qwen_cancel.setVisible(False)
            self.qwen_test.setVisible(False)
        self.qwen_download.setVisible(not installed)
        self.qwen_download.setEnabled(not installed)

    def _set_qwen_download_controls_enabled(self, enabled: bool) -> None:
        self.qwen_model.setEnabled(enabled)
        self.qwen_model_path.setEnabled(enabled)
        self.qwen_browse.setEnabled(enabled)
        self.qwen_test.setEnabled(enabled)

    def _set_qwen_row_visible(self, row_name: str, visible: bool) -> None:
        if row_name == "download":
            row_widget = getattr(self, "_qwen_download_row", None)
            label = getattr(self, "_qwen_download_label", None)
        elif row_name == "progress":
            row_widget = self.qwen_progress
            label = getattr(self, "_qwen_progress_label", None)
        else:
            return
        if row_widget is not None:
            row_widget.setVisible(visible)
        if label is not None:
            label.setVisible(visible)

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
            stopped = False
        test_thread = getattr(self, "_test_thread", None)
        if test_thread is not None and test_thread.isRunning():
            test_worker = getattr(self, "_test_worker", None)
            cancel = getattr(test_worker, "cancel", None)
            if callable(cancel):
                cancel()
            test_thread.requestInterruption()
            test_thread.quit()
            stopped = False
        download_thread = getattr(self, "_qwen_download_thread", None)
        if download_thread is not None and download_thread.isRunning():
            download_worker = getattr(self, "_qwen_download_worker", None)
            cancel = getattr(download_worker, "cancel", None)
            if callable(cancel):
                cancel(remove_partial=False)
            download_thread.requestInterruption()
            download_thread.quit()
            stopped = False
        if not stopped:
            QMessageBox.information(
                self,
                "Finishing Settings work",
                "Hydra is stopping Settings background work. Please close Settings again in a moment.",
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
