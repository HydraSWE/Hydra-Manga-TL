"""Import-progress UI shown while a manga project is prepared."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from hydra_manga_tl.core.assets import find_asset
from hydra_manga_tl.project.import_scan import ImportScanResult
from hydra_manga_tl.project.workspace import WORKSPACE


class ImportProgressScreen(QWidget):
    """Responsive, truthful project-preparation view shown during folder import."""

    _STAGES = ("detecting", "metadata", "preparing", "previews")
    _LABELS = {
        "detecting": "Detecting supported images",
        "metadata": "Reading file metadata",
        "preparing": "Preparing the project",
        "previews": "Loading previews",
    }

    def __init__(self) -> None:
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(80, 54, 80, 54)
        root.addStretch()

        card = QFrame()
        card.setObjectName("ImportCard")
        card.setMaximumWidth(780)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        logo = QLabel()
        logo.setObjectName("ImportLogo")
        logo.setFixedSize(58, 58)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_path = find_asset("thumbnail", "hydra.png")
        logo_pixmap = QPixmap(str(logo_path)) if logo_path else QPixmap()
        if not logo_pixmap.isNull():
            logo.setPixmap(
                logo_pixmap.scaled(
                    52,
                    52,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

        title_column = QVBoxLayout()
        title_column.setSpacing(3)
        title = QLabel("Preparing Translation Project")
        title.setObjectName("ImportTitle")
        subtitle = QLabel("Hydra is scanning sources and building a safe local workspace.")
        subtitle.setObjectName("Muted")
        title_column.addWidget(title)
        title_column.addWidget(subtitle)
        header.addWidget(logo)
        header.addLayout(title_column, 1)
        layout.addLayout(header)

        self.project_name = QLabel()
        self.project_name.setObjectName("ImportProjectName")
        layout.addWidget(self.project_name)

        self.stage_labels: dict[str, QLabel] = {}
        self.stage_marks: dict[str, QLabel] = {}
        self.stage_rows: dict[str, QFrame] = {}
        stages = QFrame()
        stages.setObjectName("ImportStages")
        stages_layout = QVBoxLayout(stages)
        stages_layout.setContentsMargins(0, 0, 0, 0)
        stages_layout.setSpacing(6)
        for stage in self._STAGES:
            row = QFrame()
            row.setObjectName("ImportStageRow")
            row.setProperty("stageState", "pending")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(10, 7, 10, 7)
            row_layout.setSpacing(9)
            mark = QLabel("○")
            mark.setObjectName("ImportStageMark")
            mark.setFixedWidth(20)
            label = QLabel(self._LABELS[stage])
            label.setObjectName("ImportStageLabel")
            self.stage_labels[stage] = label
            self.stage_marks[stage] = mark
            self.stage_rows[stage] = row
            row_layout.addWidget(mark)
            row_layout.addWidget(label, 1)
            stages_layout.addWidget(row)
        layout.addWidget(stages)

        self.progress = QProgressBar()
        self.progress.setObjectName("ImportProgressBar")
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)

        self.detail = QLabel()
        self.detail.setObjectName("ImportDetail")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

        self.summary = QLabel()
        self.summary.setObjectName("ImportSummary")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        host = QHBoxLayout()
        host.addStretch()
        host.addWidget(card)
        host.addStretch()
        root.addLayout(host)
        root.addStretch()

    def begin(self, paths: list[Path]) -> None:
        name = WORKSPACE._default_project_name(paths)
        self.project_name.setText(name)
        self.summary.clear()
        self.update_progress("detecting", 0, 0, "Scanning folders...")

    def update_progress(self, stage: str, current: int, total: int, detail: str) -> None:
        active_index = self._STAGES.index(stage)
        for index, value in enumerate(self._STAGES):
            state = "complete" if index < active_index else ("active" if index == active_index else "pending")
            marker = "✓" if state == "complete" else ("●" if state == "active" else "○")
            self.stage_marks[value].setText(marker)
            self.stage_rows[value].setProperty("stageState", state)
            self.stage_rows[value].style().unpolish(self.stage_rows[value])
            self.stage_rows[value].style().polish(self.stage_rows[value])
            self.stage_labels[value].setProperty("active", state == "active")
            self.stage_labels[value].style().unpolish(self.stage_labels[value])
            self.stage_labels[value].style().polish(self.stage_labels[value])

        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(current)
            self.progress.setFormat(f"{current} / {total}")
        else:
            self.progress.setRange(0, 0)
            self.progress.setFormat("")

        self.detail.setText(detail)

    def show_result(self, result: ImportScanResult) -> None:
        format_text = "  •  ".join(f"{name} {count}" for name, count in sorted(result.formats.items()))
        size = self._format_bytes(result.total_bytes)
        skipped = f"  •  {len(result.unreadable)} unreadable skipped" if result.unreadable else ""
        self.summary.setText(
            f"{result.image_count} images  •  {size}  •  Average {result.average_width} × {result.average_height} px\n"
            f"{format_text}{skipped}"
        )
        self.update_progress("preparing", 0, 0, "Creating the project...")

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(value)
        for unit in ("B", "KB", "MB", "GB"):
            if amount < 1024 or unit == "GB":
                return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
            amount /= 1024
        return f"{amount:.1f} GB"
