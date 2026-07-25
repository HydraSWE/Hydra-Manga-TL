"""Central desktop application controller."""

from __future__ import annotations

import ctypes
import logging
import sys
from pathlib import Path

from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .paths import PATHS
from .ocr_runtime import shutdown_ocr_runtime, shutdown_ocr_warmup, start_ocr_runtime, start_ocr_warmup
from .render_queue import shutdown_render_queue
from .theme import STYLESHEET
from .translation_runtime import shutdown_translation_runtime, shutdown_translation_warmup, start_translation_warmup
from .translation_queue import shutdown_translation_queue
from .ui import MainWindow


APP_USER_MODEL_ID = "Hydra.MangaTL.0.8.0"


class MangaApplication:
    def __init__(self, startup_path: Path | None = None) -> None:
        self._configure_windows_identity()
        self.qt_app = QApplication.instance() or QApplication(sys.argv)
        self.startup_path = startup_path
        self.main_window: MainWindow | None = None
        self._brand_icon_path: Path | None = None
        self._native_icon_handles: list[int] = []
        self._configure_application()

    @staticmethod
    def _configure_windows_identity() -> None:
        if sys.platform == "win32":
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
            except (AttributeError, OSError):
                pass

    def _configure_application(self) -> None:
        self.qt_app.setApplicationName("Hydra Manga TL")
        self.qt_app.setApplicationVersion("0.8.0-alpha")
        self.qt_app.setOrganizationName("Hydra")
        QFontDatabase.addApplicationFont(r"C:\Windows\Fonts\segoeui.ttf")
        self._brand_icon_path = self._brand_icon_file()
        self.qt_app.setWindowIcon(self._load_brand_icon())
        self.qt_app.setStyleSheet(STYLESHEET)

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
        roots = [
            Path(__file__).resolve().parent.parent,
            Path(getattr(sys, "_MEIPASS", "") or ".").resolve(),
            Path(sys.executable).resolve().parent,
            Path(sys.executable).resolve().parent / "_internal",
        ]
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            key = str(root).casefold()
            if key not in seen:
                seen.add(key)
                unique.append(root)
        return unique

    def initialize(self) -> None:
        PATHS.initialize()
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[logging.FileHandler(PATHS.logs / "app.log", encoding="utf-8"), logging.StreamHandler()],
            force=True,
        )
        logging.getLogger(__name__).info("Starting Hydra Manga TL 0.8.0-alpha")
        if SETTINGS.ocr_subprocess_enabled:
            start_ocr_runtime(
                memory_limit_mb=SETTINGS.ocr_worker_memory_limit_mb,
                recycle_pages=SETTINGS.ocr_worker_recycle_pages,
            )
        else:
            start_ocr_warmup()
        start_translation_warmup(translation_engine=SETTINGS.translation_engine)

    def create_main_window(self) -> MainWindow:
        self.main_window = MainWindow()
        self._apply_window_icon(self.main_window)
        return self.main_window

    def run(self) -> int:
        self.initialize(); window = self.create_main_window(); window.show()
        QTimer.singleShot(0, lambda: self._apply_window_icon(window))
        self.qt_app.aboutToQuit.connect(WORKSPACE.shutdown)
        self.qt_app.aboutToQuit.connect(shutdown_translation_queue)
        self.qt_app.aboutToQuit.connect(shutdown_render_queue)
        self.qt_app.aboutToQuit.connect(shutdown_translation_runtime)
        self.qt_app.aboutToQuit.connect(shutdown_ocr_runtime)
        self.qt_app.aboutToQuit.connect(shutdown_ocr_warmup)
        self.qt_app.aboutToQuit.connect(shutdown_translation_warmup)
        if self.startup_path is not None: window.open_startup_path(self.startup_path)
        elif WORKSPACE.recent_projects():
            window.open_project(WORKSPACE.recent_projects()[0])
        return self.qt_app.exec()


from .workspace import WORKSPACE  # imported late to keep bootstrap dependencies explicit
from .settings import SETTINGS
