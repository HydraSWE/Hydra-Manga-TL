"""IdentityPreviewDialog implementation."""

from __future__ import annotations

from .common import *  # noqa: F401,F403


class IdentityPreviewDialog(QDialog):
    def __init__(self, image_path: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Hydra Manga TL")
        self.resize(760, 800)
        self.setMinimumSize(420, 460)
        self._source = QPixmap(str(image_path))
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)
        
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setObjectName("IdentityPreview")
        layout.addWidget(self.preview, 1)
        
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        
        self._scale_preview()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._scale_preview()

    def _scale_preview(self) -> None:
        if self._source.isNull():
            self.preview.setText("Hydra identity artwork is unavailable.")
            return
        available = self.preview.size() - QSize(16, 16)
        if available.width() <= 0 or available.height() <= 0:
            return
        self.preview.setPixmap(
            self._source.scaled(
                available,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


