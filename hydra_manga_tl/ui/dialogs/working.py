"""WorkingDialog implementation."""

from __future__ import annotations

from .common import *  # noqa: F401,F403


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


