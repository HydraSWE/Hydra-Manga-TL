from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QSizePolicy
)

class StartupCoordinator(QObject):
    """Publish monotonic startup state without owning heavyweight services."""

    progress_changed = Signal(str, str, int)
    warning = Signal(str)
    fatal_error = Signal(str)
    completed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._progress = 0

    @property
    def progress(self) -> int:
        return self._progress

    def advance(self, stage: str, label: str, progress: int) -> None:
        self._progress = max(self._progress, min(100, int(progress)))
        self.progress_changed.emit(str(stage), str(label), self._progress)
        app = QApplication.instance()
        if app is not None:
            app.processEvents()


class StartupSplash(QWidget):
    """Frameless branded splash that remains useful when startup fails."""

    def __init__(self, logo_path: Path | None, version: str) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setObjectName("StartupSplash")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setFixedSize(660, 430)
        
        # Kept original colors and theme intact. 
        # Refined height and border-radius slightly for a sleeker progress bar.
        self.setStyleSheet(
            """
            QWidget#StartupSplash {
                background: #080d17;
                border: 1px solid #1c63b7;
                border-radius: 18px;
            }
            QLabel { color: #eaf5ff; }
            QLabel#SplashMuted { color: #8192a8; }
            QLabel#SplashStatus { color: #79cfff; font-size: 14px; font-weight: bold; }
            QProgressBar {
                background: #111b2a; border: 1px solid #244463;
                border-radius: 6px; height: 10px; text-align: center;
            }
            QProgressBar::chunk {
                border-radius: 5px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #176bd1, stop:1 #42d8ff);
            }
            QPushButton {
                color: white; background: #176bd1; border: 0;
                border-radius: 6px; padding: 8px 20px;
            }
            """
        )
        
        # Main Layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 34, 34, 26)
        layout.setSpacing(0)

        # --- Top Section: Logo & Version ---
        self.logo = QLabel()
        self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if logo_path is not None:
            pixmap = QPixmap(str(logo_path))
            if not pixmap.isNull():
                self.logo.setPixmap(
                    pixmap.scaled(
                        520,
                        220,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
        layout.addWidget(self.logo)

        version_label = QLabel(f"Version {version}")
        version_label.setObjectName("SplashMuted")
        version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(version_label)

        # Spacer to push loading UI to the bottom
        layout.addStretch(1)

        # --- Bottom Section: Enhanced Loading UI ---
        loading_layout = QVBoxLayout()
        loading_layout.setSpacing(8)

        # Header: Status (Left) and Percentage (Right)
        status_header_layout = QHBoxLayout()
        self.status = QLabel("Starting Hydra…")
        self.status.setObjectName("SplashStatus")
        
        self.percentage_label = QLabel("0%")
        self.percentage_label.setObjectName("SplashStatus")
        self.percentage_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        
        status_header_layout.addWidget(self.status)
        status_header_layout.addWidget(self.percentage_label)
        loading_layout.addLayout(status_header_layout)

        # Progress Bar
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)  # Hidden standard text in favor of custom label
        loading_layout.addWidget(self.progress)

        # History Footer
        self.history = QLabel("")
        self.history.setObjectName("SplashMuted")
        self.history.setWordWrap(True)
        self.history.setMinimumHeight(38)
        self.history.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        loading_layout.addWidget(self.history)

        layout.addLayout(loading_layout)

        # --- Error / Exit Section ---
        bottom_actions = QHBoxLayout()
        bottom_actions.addStretch()
        self.exit_button = QPushButton("Exit")
        self.exit_button.hide()
        self.exit_button.clicked.connect(QApplication.quit)
        bottom_actions.addWidget(self.exit_button)
        layout.addLayout(bottom_actions)
        
        self._completed_labels: list[str] = []

    def show_centered(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.move(screen.availableGeometry().center() - self.rect().center())
        self.show()
        self.raise_()
        QApplication.processEvents()

    def update_progress(self, stage: str, label: str, progress: int) -> None:
        del stage
        previous = self.status.text()
        if previous and previous != "Starting Hydra…" and previous != label:
            self._completed_labels.append(previous.rstrip(".") + " ✓")
            self._completed_labels = self._completed_labels[-3:]
            
        self.progress.setValue(progress)
        self.percentage_label.setText(f"{progress}%")
        self.status.setText(label)
        self.history.setText("   ".join(self._completed_labels))

    def show_warning(self, message: str) -> None:
        self.history.setText(message)

    def show_fatal_error(self, message: str) -> None:
        self.progress.hide()
        self.percentage_label.hide()
        self.status.setText("Hydra could not start")
        self.history.setText(message)
        self.exit_button.show()
        self.show_centered()