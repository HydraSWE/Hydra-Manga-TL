"""Central desktop application controller."""

from __future__ import annotations

import ctypes
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from hydra_manga_tl import __version__
from hydra_manga_tl.core.assets import asset_roots, find_asset
from hydra_manga_tl.core.paths import PATHS
from hydra_manga_tl.core.startup import StartupCoordinator, StartupSplash

if TYPE_CHECKING:
    from hydra_manga_tl.core.settings import AppSettings
    from hydra_manga_tl.ui import MainWindow


APP_USER_MODEL_ID = "Hydra.MangaTL"


class MangaApplication:
    def __init__(self, startup_path: Path | None = None) -> None:
        self._configure_windows_identity()
        self.qt_app = QApplication.instance() or QApplication(sys.argv)
        self.startup_path = startup_path
        self.main_window: MainWindow | None = None
        self.settings: AppSettings | None = None
        self._brand_icon_path: Path | None = None
        self._native_icon_handles: list[int] = []
        self._shutdown_callbacks: list[object] = []
        self._startup_warnings: list[str] = []
        self._configure_application_identity()
        self.startup = StartupCoordinator()
        self.splash = StartupSplash(find_asset("logos", "mainlogo.png"), __version__)
        self.startup.progress_changed.connect(self.splash.update_progress)
        self.startup.warning.connect(self.splash.show_warning)
        self.startup.warning.connect(self._record_startup_warning)
        self.startup.fatal_error.connect(self.splash.show_fatal_error)
        self.splash.show_centered()

    def _record_startup_warning(self, message: str) -> None:
        if message and message not in self._startup_warnings:
            self._startup_warnings.append(message)

    @staticmethod
    def _configure_windows_identity() -> None:
        if sys.platform == "win32":
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
            except (AttributeError, OSError):
                pass

    def _configure_application_identity(self) -> None:
        self.qt_app.setApplicationName("Hydra Manga TL")
        self.qt_app.setApplicationVersion(__version__)
        self.qt_app.setOrganizationName("Hydra")
        QFontDatabase.addApplicationFont(r"C:\Windows\Fonts\segoeui.ttf")
        self._brand_icon_path = self._brand_icon_file()
        self.qt_app.setWindowIcon(self._load_brand_icon())

    @classmethod
    def _brand_icon_file(cls) -> Path | None:
        for root in MangaApplication._asset_roots():
            for path in (
                root / "assets" / "icons" / "app.ico",
                root / "assets" / "icons" / "app_icon.png",
            ):
                if path.is_file():
                    return path
        return None

    @classmethod
    def _load_brand_icon(cls) -> QIcon:
        path = cls._brand_icon_file()
        if path is not None:
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
        return QIcon()

    def _apply_window_icon(self, window: MainWindow) -> None:
        icon = self.qt_app.windowIcon()
        if not icon.isNull():
            window.setWindowIcon(icon)
        self._apply_native_windows_icon(window)

    def _apply_native_windows_icon(self, window: MainWindow) -> None:
        if sys.platform != "win32" or self._brand_icon_path is None:
            return
        try:
            hwnd = int(window.winId())
            user32 = ctypes.windll.user32
            user32.LoadImageW.restype = ctypes.c_void_p
            user32.LoadImageW.argtypes = [
                ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                ctypes.c_int, ctypes.c_int, ctypes.c_uint,
            ]
            user32.SendMessageW.restype = ctypes.c_void_p
            user32.SendMessageW.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p,
            ]
            big_icon = user32.LoadImageW(
                None, str(self._brand_icon_path), 1, 256, 256, 0x00000010
            )
            small_w = user32.GetSystemMetrics(49)
            small_h = user32.GetSystemMetrics(50)
            small_icon = user32.LoadImageW(
                None, str(self._brand_icon_path), 1, small_w, small_h, 0x00000010
            )
            if big_icon:
                self._native_icon_handles.append(int(big_icon))
                user32.SendMessageW(hwnd, 0x0080, 1, big_icon)
                self._set_window_class_icon(hwnd, -14, big_icon)
            if small_icon:
                self._native_icon_handles.append(int(small_icon))
                user32.SendMessageW(hwnd, 0x0080, 0, small_icon)
                self._set_window_class_icon(hwnd, -34, small_icon)
        except (AttributeError, OSError, TypeError, ValueError):
            logging.getLogger(__name__).debug("Unable to apply native Windows icon", exc_info=True)

    @staticmethod
    def _set_window_class_icon(hwnd: int, index: int, icon_handle: int) -> None:
        user32 = ctypes.windll.user32
        setter = getattr(user32, "SetClassLongPtrW", None) or getattr(user32, "SetClassLongW", None)
        if setter is None:
            return
        setter.restype = ctypes.c_void_p
        setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        setter(hwnd, index, icon_handle)

    @staticmethod
    def _asset_roots() -> list[Path]:
        return asset_roots()

    def initialize(self) -> None:
        self.startup.advance("core", "Loading settings and workspace paths…", 8)
        from hydra_manga_tl.core.settings import CREDENTIALS, PROVIDER_ENV, SETTINGS

        self.settings = SETTINGS
        PATHS.initialize()
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[logging.FileHandler(PATHS.logs / "app.log", encoding="utf-8"), logging.StreamHandler()],
            force=True,
        )
        logging.getLogger(__name__).info("Starting Hydra Manga TL %s", __version__)

        self.startup.advance("appearance", "Initializing theme, fonts, and assets…", 22)
        from hydra_manga_tl.core.theme import STYLESHEET

        self.qt_app.setStyleSheet(STYLESHEET)
        QFontDatabase.families()

        self.startup.advance("providers", "Checking configured providers…", 34)
        selected_cloud = {
            SETTINGS.translation_engine,
            SETTINGS.literal_provider,
            SETTINGS.localization_provider,
            SETTINGS.reconstruction_analysis_provider,
        } & set(PROVIDER_ENV)
        missing = [provider for provider in sorted(selected_cloud) if not CREDENTIALS.get(provider)]
        if missing:
            self.startup.warning.emit(
                "Cloud credentials are not configured for: " + ", ".join(missing)
            )

        self.startup.advance("renderer", "Preparing renderer and reconstruction providers…", 46)
        from hydra_manga_tl.phase import renderer as _renderer
        from hydra_manga_tl.title.reconstruction import (
            RECONSTRUCTION_ANALYSIS_PROVIDER_REGISTRY,
            RECONSTRUCTION_PROVIDER_REGISTRY,
        )
        del _renderer
        if SETTINGS.title_reconstruction_provider not in RECONSTRUCTION_PROVIDER_REGISTRY:
            self.startup.warning.emit(
                f"Unknown reconstruction provider: {SETTINGS.title_reconstruction_provider}"
            )
        if SETTINGS.reconstruction_analysis_provider not in RECONSTRUCTION_ANALYSIS_PROVIDER_REGISTRY:
            self.startup.warning.emit(
                f"Unknown reconstruction analysis provider: {SETTINGS.reconstruction_analysis_provider}"
            )

    def start_background_warmup(self) -> None:
        from hydra_manga_tl.ocr.runtime import start_ocr_runtime, start_ocr_warmup
        from hydra_manga_tl.translation.runtime import start_translation_warmup

        assert self.settings is not None
        settings = self.settings
        if settings.ocr_subprocess_enabled:
            start_ocr_runtime(
                memory_limit_mb=settings.ocr_worker_memory_limit_mb,
                recycle_pages=settings.ocr_worker_recycle_pages,
            )
        else:
            start_ocr_warmup()
        start_translation_warmup(
            translation_engine=settings.translation_engine,
            config={
                "translation_engine": settings.translation_engine,
                "translation_fallback_engine": settings.translation_fallback_engine,
                "qwen_model_path": settings.qwen_model_path,
                "qwen_model_name": settings.qwen_model_name,
            },
        )

    def create_main_window(self) -> MainWindow:
        from hydra_manga_tl.ui import MainWindow

        self.main_window = MainWindow()
        self._apply_window_icon(self.main_window)
        return self.main_window

    def run(self) -> int:
        try:
            self.initialize()
            self.startup.advance("ui", "Building workspace interface…", 62)
            window = self.create_main_window()
            self.startup.advance("workspace", "Restoring workspace…", 78)
            from hydra_manga_tl.project.workspace import WORKSPACE

            if self.startup_path is not None:
                window.open_startup_path(self.startup_path)
            elif WORKSPACE.recent_projects():
                window.open_project(WORKSPACE.recent_projects()[0])
            self.startup.advance("warmup", "Starting OCR and translation warmup…", 90)
            self.start_background_warmup()
            self._connect_shutdown(WORKSPACE)
        except Exception as error:
            logging.getLogger(__name__).exception("Fatal startup failure")
            self.startup.fatal_error.emit(str(error) or type(error).__name__)
            return self.qt_app.exec()

        self.startup.advance("ready", "Ready", 100)
        self.startup.completed.emit()
        window.show()
        QTimer.singleShot(0, lambda: self._apply_window_icon(window))
        QTimer.singleShot(80, self.splash.close)
        status = "Workspace ready • OCR and local model warmup may continue in the background"
        if self._startup_warnings:
            status += " • " + self._startup_warnings[-1]
        window.statusBar().showMessage(status, 10000)
        return self.qt_app.exec()

    def _connect_shutdown(self, workspace) -> None:
        from hydra_manga_tl.ocr.runtime import shutdown_ocr_runtime, shutdown_ocr_warmup
        from hydra_manga_tl.phase.render_queue import shutdown_render_queue
        from hydra_manga_tl.translation.queue import shutdown_translation_queue
        from hydra_manga_tl.translation.runtime import (
            shutdown_translation_runtime,
            shutdown_translation_warmup,
        )

        callbacks = (
            workspace.shutdown,
            shutdown_translation_queue,
            shutdown_render_queue,
            shutdown_translation_runtime,
            shutdown_ocr_runtime,
            shutdown_ocr_warmup,
            shutdown_translation_warmup,
        )
        self._shutdown_callbacks = list(callbacks)
        for callback in callbacks:
            self.qt_app.aboutToQuit.connect(callback)
