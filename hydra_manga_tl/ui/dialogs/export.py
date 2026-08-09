"""ExportOptionsDialog implementation."""

from __future__ import annotations

from .common import *  # noqa: F401,F403


class ExportOptionsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export")
        self.setModal(True)
        self.setFixedWidth(420)

        self.output_type = QComboBox()
        self.output_type.addItem("Image folder", "folder")
        self.output_type.addItem("ZIP archive", "zip")
        self.output_type.addItem("CBZ comic archive", "cbz")
        self.output_type.addItem("PDF document", "pdf")

        self.image_format = QComboBox()
        self.image_format.addItem("PNG", "png")
        self.image_format.addItem("JPEG", "jpg")
        self.image_format.addItem("WebP", "webp")
        self.output_type.currentIndexChanged.connect(
            lambda: self.image_format.setEnabled(
                self.output_type.currentData() != "pdf"
            )
        )

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        form.addRow("Output", self.output_type)
        form.addRow("Image format", self.image_format)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Export")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(20)
        layout.addLayout(form)
        layout.addWidget(buttons)


