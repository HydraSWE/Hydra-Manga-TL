from __future__ import annotations

from PySide6.QtCore import QPropertyAnimation, Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)


class OverlayProgressWidget(QFrame):
    cancel_requested = Signal(str)
    retry_requested = Signal(str)
    finished = Signal(str)

    def __init__(
        self,
        request_id: str,
        parent = None,
        title: str = "Hydra",
        operation: str = "Translating Selection...",
        stages: list[str] | None = None,
        is_compact: bool = False,
    ) -> None:
        super().__init__(parent)
        self.request_id = request_id
        self.operation_text = operation
        self.stage_names = stages or ["OCR", "Translation", "Rendering"]
        self.is_compact = is_compact
        self.stages_state: dict[str, str] = {stage: "waiting" for stage in self.stage_names}
        self.is_cancelled = False

        self.setObjectName("OverlayProgressCard")
        self.setFixedWidth(240)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.set_theme(False)

        # Drop shadow for soft elevation
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(16)
        shadow.setColor(QColor(0, 0, 0, 160))
        shadow.setOffset(0, 4)
        self.setGraphicsEffect(shadow)

        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)
        self._opacity_effect.setOpacity(1.0)

        self.layout_widget = QVBoxLayout(self)
        self.layout_widget.setContentsMargins(14, 12, 14, 12)
        self.layout_widget.setSpacing(8)

        # Title Label
        self.title_label = QLabel(title)
        self.title_label.setObjectName("Title")
        self.layout_widget.addWidget(self.title_label)

        # Operation Status Label
        self.op_label = QLabel(operation)
        self.op_label.setObjectName("Operation")
        self.op_label.setWordWrap(True)
        self.layout_widget.addWidget(self.op_label)

        # Progress Bar (Subtle)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0) # Indeterminate initially
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.layout_widget.addWidget(self.progress_bar)

        # Stages list layout
        self.stages_container = QVBoxLayout()
        self.stages_container.setSpacing(4)
        self.stage_labels: dict[str, QLabel] = {}

        if not self.is_compact:
            for stage in self.stage_names:
                stage_row = QHBoxLayout()
                stage_row.setSpacing(6)
                
                # We use properties to style running/completed states dynamically
                lbl = QLabel(f"○ {stage}")
                lbl.setObjectName("Stage")
                lbl.setProperty("state", "waiting")
                
                self.stage_labels[stage] = lbl
                stage_row.addWidget(lbl)
                self.stages_container.addLayout(stage_row)
            self.layout_widget.addLayout(self.stages_container)

        # Buttons (Cancel & Retry)
        self.button_layout = QHBoxLayout()
        self.button_layout.setSpacing(6)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("CancelBtn")
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        self.button_layout.addWidget(self.cancel_button)

        self.retry_button = QPushButton("Retry")
        self.retry_button.clicked.connect(self._on_retry_clicked)
        self.retry_button.hide()
        self.button_layout.addWidget(self.retry_button)

        self.layout_widget.addLayout(self.button_layout)
        self.adjustSize()

    def set_compact(self, is_compact: bool) -> None:
        if self.is_compact == is_compact:
            return
        self.is_compact = is_compact
        if is_compact:
            for label in self.stage_labels.values():
                label.hide()
        else:
            for label in self.stage_labels.values():
                label.show()
        self.adjustSize()

    def update_stage(self, stage_name: str, state: str) -> None:
        """state can be: waiting, running, completed"""
        if stage_name not in self.stages_state:
            return
        self.stages_state[stage_name] = state
        if not self.is_compact and stage_name in self.stage_labels:
            lbl = self.stage_labels[stage_name]
            lbl.setProperty("state", state)
            lbl.style().unpolish(lbl)
            lbl.style().polish(lbl)

            indicator = "○"
            if state == "running":
                indicator = "●"
            elif state == "completed":
                indicator = "✓"
            lbl.setText(f"{indicator} {stage_name}")

    def set_operation_text(self, text: str) -> None:
        self.op_label.setText(text)

    def set_progress(self, value: int, total: int) -> None:
        if total <= 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(value)

    def show_success(self, message: str = "✓ Translation Complete") -> None:
        self.op_label.setText(message)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.cancel_button.hide()
        self.retry_button.hide()
        if not self.is_compact:
            for stage in self.stage_names:
                self.update_stage(stage, "completed")
        self.adjustSize()

    def show_failure(self, message: str = "⚠ Translation Failed") -> None:
        self.op_label.setText(message)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.cancel_button.setText("Cancel")
        self.cancel_button.show()
        self.retry_button.show()
        self.adjustSize()

    def show_cancelled(self) -> None:
        self.op_label.setText("Cancelled")
        self.cancel_button.hide()
        self.retry_button.hide()
        self.adjustSize()

    def start_fade_out(self, duration_ms: int = 250) -> None:
        self.anim = QPropertyAnimation(self._opacity_effect, b"opacity")
        self.anim.setDuration(duration_ms)
        self.anim.setStartValue(1.0)
        self.anim.setEndValue(0.0)
        self.anim.finished.connect(self._on_fade_out_finished)
        self.anim.start()

    def _on_fade_out_finished(self) -> None:
        self.finished.emit(self.request_id)
        self.deleteLater()

    @Slot()
    def _on_cancel_clicked(self) -> None:
        if self.is_cancelled:
            return
        self.is_cancelled = True
        self.cancel_requested.emit(self.request_id)
        self.start_fade_out(200)

    @Slot()
    def _on_retry_clicked(self) -> None:
        self.retry_button.hide()
        self.cancel_button.setText("Cancel")
        self.op_label.setText(self.operation_text)
        self.progress_bar.setRange(0, 0)
        if not self.is_compact:
            for stage in self.stage_names:
                self.update_stage(stage, "waiting")
        self.retry_requested.emit(self.request_id)

    def set_theme(self, light_theme: bool) -> None:
        if light_theme:
            self.setStyleSheet("""
                QFrame#OverlayProgressCard {
                    background-color: rgba(240, 244, 250, 242);
                    border: 1px solid #2864dc;
                    border-radius: 12px;
                }
                QLabel {
                    color: #11151c;
                    font-family: 'Segoe UI';
                    background: transparent;
                }
                QLabel#Title {
                    color: #2864dc;
                    font-weight: 700;
                    font-size: 11pt;
                }
                QLabel#Operation {
                    color: #11151c;
                    font-weight: 600;
                    font-size: 9.5pt;
                }
                QLabel#Stage {
                    color: #526073;
                    font-size: 9.5pt;
                }
                QLabel#Stage[state="running"] {
                    color: #2864dc;
                    font-weight: 600;
                }
                QLabel#Stage[state="completed"] {
                    color: #248a46;
                }
                QProgressBar {
                    background: #dce7f7;
                    border: 1px solid #b9c7da;
                    border-radius: 4px;
                    text-align: center;
                    height: 6px;
                }
                QProgressBar::chunk {
                    background: #2864dc;
                    border-radius: 3px;
                }
                QPushButton {
                    background: #e1e7f0;
                    border: 1px solid #b9c7da;
                    border-radius: 6px;
                    color: #11151c;
                    padding: 4px 10px;
                    font-size: 9pt;
                }
                QPushButton:hover {
                    background: #d2dceb;
                    border-color: #92a4ba;
                }
                QPushButton#CancelBtn {
                    color: #cc2929;
                    border-color: #f5baba;
                }
                QPushButton#CancelBtn:hover {
                    background: #fce8e8;
                    border-color: #cc2929;
                }
            """)
        else:
            self.setStyleSheet("""
                QFrame#OverlayProgressCard {
                    background-color: rgba(17, 24, 33, 242);
                    border: 1px solid #4d83ff;
                    border-radius: 12px;
                }
                QLabel {
                    color: #e8edf5;
                    font-family: 'Segoe UI';
                    background: transparent;
                }
                QLabel#Title {
                    color: #4d83ff;
                    font-weight: 700;
                    font-size: 11pt;
                }
                QLabel#Operation {
                    color: #e8edf5;
                    font-weight: 600;
                    font-size: 9.5pt;
                }
                QLabel#Stage {
                    color: #8f9bad;
                    font-size: 9.5pt;
                }
                QLabel#Stage[state="running"] {
                    color: #4d83ff;
                    font-weight: 600;
                }
                QLabel#Stage[state="completed"] {
                    color: #4dcb7b;
                }
                QProgressBar {
                    background: #171d27;
                    border: 1px solid #263141;
                    border-radius: 4px;
                    text-align: center;
                    height: 6px;
                }
                QProgressBar::chunk {
                    background: #4d83ff;
                    border-radius: 3px;
                }
                QPushButton {
                    background: #202a38;
                    border: 1px solid #303d50;
                    border-radius: 6px;
                    color: #e8edf5;
                    padding: 4px 10px;
                    font-size: 9pt;
                }
                QPushButton:hover {
                    background: #29364a;
                    border-color: #4b6382;
                }
                QPushButton#CancelBtn {
                    color: #ff7272;
                    border-color: #5b2d35;
                }
                QPushButton#CancelBtn:hover {
                    background: #3a1e25;
                    border-color: #ff7272;
                }
            """)
        for child in self.findChildren(QLabel) + self.findChildren(QPushButton) + self.findChildren(QProgressBar):
            child.style().unpolish(child)
            child.style().polish(child)
