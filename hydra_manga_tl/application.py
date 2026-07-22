"""Central desktop application controller."""

from __future__ import annotations

import ctypes
import logging
import sys
from pathlib import Path

from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication

from .paths import PATHS
from .ocr_runtime import shutdown_ocr_warmup, start_ocr_warmup
from .theme import STYLESHEET
from .translation_runtime import shutdown_translation_warmup, start_translation_warmup
from .ui import MainWindow


class MangaApplication:
    def __init__(self, startup_path: Path | None = None) -> None:
        self._configure_windows_identity()
        self.qt_app = QApplication.instance() or QApplication(sys.argv)
        self.startup_path = startup_path
        self.main_window: MainWindow | None = None
        self._configure_application()

    @staticmethod
    def _configure_windows_identity() -> None:
        if sys.platform == "win32":
            try: ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Hydra.MangaTL.0.5")
            except (AttributeError, OSError): pass

    def _configure_application(self) -> None:
        self.qt_app.setApplicationName("Hydra Manga TL")
        self.qt_app.setApplicationVersion("0.6.0")
        self.qt_app.setOrganizationName("Hydra")
        QFontDatabase.addApplicationFont(r"C:\Windows\Fonts\segoeui.ttf")
        self.qt_app.setWindowIcon(self._load_brand_icon())
        self.qt_app.setStyleSheet(STYLESHEET)

    @staticmethod
    def _load_brand_icon() -> QIcon:
        root = Path(__file__).resolve().parent.parent
        icon_candidates = [
            root / "assets" / "icons" / "app.ico",
            root / "assets" / "icons" / "app_icon.png",
        ]
        for path in icon_candidates:
            if path.is_file():
                icon = QIcon(str(path))
                if not icon.isNull():
                    return icon
        return QIcon()

    def initialize(self) -> None:
        PATHS.initialize()
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[logging.FileHandler(PATHS.logs / "app.log", encoding="utf-8"), logging.StreamHandler()],
            force=True,
        )
        logging.getLogger(__name__).info("Starting Hydra Manga TL 0.6.0")
        start_ocr_warmup()
        start_translation_warmup()

    def create_main_window(self) -> MainWindow:
        self.main_window = MainWindow(); return self.main_window

    def run(self) -> int:
        self.initialize(); window = self.create_main_window(); window.show()
        self.qt_app.aboutToQuit.connect(WORKSPACE.shutdown)
        self.qt_app.aboutToQuit.connect(shutdown_ocr_warmup)
        self.qt_app.aboutToQuit.connect(shutdown_translation_warmup)
        if self.startup_path is not None: window.open_startup_path(self.startup_path)
        elif WORKSPACE.recent_projects():
            window.open_project(WORKSPACE.recent_projects()[0])
        return self.qt_app.exec()


from .workspace import WORKSPACE  # imported late to keep bootstrap dependencies explicit
