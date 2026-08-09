"""BackgroundWorkDialog implementation."""

from __future__ import annotations

from .common import *  # noqa: F401,F403


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
