"""Shared Qt helpers for Hydra Manga TL UI modules."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPolygonF, QPixmap
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QComboBox, QToolButton, QVBoxLayout, QWidget

from hydra_manga_tl.core.assets import find_asset


TARGET_LANGUAGE_NAMES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
}
FILMSTRIP_CARD_SIZE = QSize(88, 104)
FILMSTRIP_PREVIEW_SIZE = QSize(72, 78)


class CollapsibleSection(QFrame):
    expanded_changed = Signal(bool)

    def __init__(self, title: str, expanded: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("InspectorSection")
        self.toggle = QToolButton()
        self.toggle.setObjectName("SectionToggle")
        self.toggle.setText(title)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.clicked.connect(self._set_expanded)

        self.body = QWidget()
        self.body.setVisible(expanded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toggle)
        layout.addWidget(self.body)

    def _set_expanded(self, expanded: bool) -> None:
        self.body.setVisible(expanded)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.expanded_changed.emit(expanded)

    def set_expanded(self, expanded: bool) -> None:
        blocked = self.toggle.blockSignals(True)
        self.toggle.setChecked(expanded)
        self.toggle.blockSignals(blocked)
        self.body.setVisible(expanded)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)


def _page_label(path: str, index: int) -> str:
    del path
    return str(index + 1)


def _language_badge(prefix: str, language: str) -> str:
    return f"{prefix} ({language})" if language else prefix


def _relative_opened_label(value: str, now: datetime | None = None) -> str:
    if not value:
        return "Last opened recently"
    try:
        opened = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "Last opened recently"
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    days = (current.astimezone().date() - opened.astimezone().date()).days
    if days == 0:
        return "Last opened Today"
    if days == 1:
        return "Last opened Yesterday"
    if 1 < days < 7:
        return f"Last opened {days} days ago"
    return f"Last opened {opened.astimezone().strftime('%b')} {opened.astimezone().day}"


def _landing_icon(kind: str, size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#4d83ff"), max(2.0, size / 18), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    if kind == "folder":
        path = QPainterPath()
        path.moveTo(size * .16, size * .32)
        path.lineTo(size * .16, size * .76)
        path.lineTo(size * .82, size * .76)
        path.lineTo(size * .88, size * .42)
        path.lineTo(size * .47, size * .42)
        path.lineTo(size * .39, size * .31)
        path.closeSubpath()
        painter.drawPath(path)
    else:
        painter.drawLine(int(size * .18), int(size * .25), int(size * .18), int(size * .78))
        painter.drawLine(int(size * .82), int(size * .25), int(size * .82), int(size * .78))
        painter.drawLine(int(size * .50), int(size * .32), int(size * .50), int(size * .82))
        left = QPainterPath(); left.moveTo(size * .18, size * .25); left.quadTo(size * .37, size * .20, size * .50, size * .32)
        right = QPainterPath(); right.moveTo(size * .82, size * .25); right.quadTo(size * .63, size * .20, size * .50, size * .32)
        painter.drawPath(left); painter.drawPath(right)
        painter.drawLine(int(size * .18), int(size * .78), int(size * .50), int(size * .82))
        painter.drawLine(int(size * .82), int(size * .78), int(size * .50), int(size * .82))
    painter.end()
    return pixmap


def lucide_icon(name: str) -> QIcon:
    path = find_asset("icons", "lucide", f"{name}.svg")
    return QIcon(str(path)) if path is not None else QIcon()


def _speaker_icon(color: str = "#69a0ff", size: int = 18) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), max(2, size // 9), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QColor(color))
    speaker = QPolygonF([
        QPointF(size * 0.12, size * 0.40),
        QPointF(size * 0.34, size * 0.40),
        QPointF(size * 0.58, size * 0.20),
        QPointF(size * 0.58, size * 0.80),
        QPointF(size * 0.34, size * 0.60),
        QPointF(size * 0.12, size * 0.60),
    ])
    painter.drawPolygon(speaker)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawArc(int(size * 0.56), int(size * 0.30), int(size * 0.26), int(size * 0.40), -45 * 16, 90 * 16)
    painter.drawArc(int(size * 0.50), int(size * 0.18), int(size * 0.44), int(size * 0.64), -45 * 16, 90 * 16)
    painter.end()
    return QIcon(pixmap)


def confirm(parent: QWidget | None, title: str, text: str) -> bool:
    from PySide6.QtWidgets import QMessageBox
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.NoIcon)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes
