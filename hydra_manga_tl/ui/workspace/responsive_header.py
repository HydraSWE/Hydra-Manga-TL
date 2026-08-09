"""Responsive header helpers for the workspace screen."""

from __future__ import annotations

import re

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QPushButton, QSizePolicy, QToolButton, QWidget


class ResponsiveHeaderMixin:
    def _register_responsive_action(self, button: QWidget) -> None:
        text = button.text() if hasattr(button, "text") else ""
        if text:
            button.setProperty("fullText", text)
            auto_tooltip = not button.toolTip()
            button.setProperty("responsiveAutoTooltip", auto_tooltip)
            if auto_tooltip:
                button.setToolTip(text)
            if not button.accessibleName():
                button.setAccessibleName(text)
        if hasattr(button, "setIconSize"):
            button.setIconSize(QSize(16, 16))
        button.setMinimumWidth(0)
        policy = button.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
        button.setSizePolicy(policy)
        self._responsive_action_buttons.append(button)
        if text:
            self._set_responsive_button_text(button, text)

    def _set_responsive_button_text(self, button: QWidget, text: str) -> None:
        button.setProperty("fullText", text)
        if text and button.property("responsiveAutoTooltip"):
            button.setToolTip(text)
        elif text and not button.toolTip():
            button.setToolTip(text)
        if text and not button.accessibleName():
            button.setAccessibleName(text)
        if isinstance(button, QToolButton):
            button.setText(text)
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonIconOnly
                if self._header_compact
                else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
        elif isinstance(button, QPushButton):
            button.setText("" if self._header_compact else text)

    def _set_header_compact(self, compact: bool) -> None:
        if compact == self._header_compact:
            return
        self._header_compact = compact
        for label in self._responsive_field_labels:
            label.setVisible(not compact)
            policy = label.sizePolicy()
            policy.setHorizontalPolicy(
                QSizePolicy.Policy.Ignored if compact else QSizePolicy.Policy.Preferred
            )
            label.setSizePolicy(policy)
        for button in self._responsive_action_buttons:
            wide_width = int(button.property("responsiveWideWidth") or 156)
            wide_min_width = int(button.property("responsiveMinWidth") or 36)
            scope = str(button.property("responsiveScope") or "header")
            if scope == "toolstrip":
                compact_width = int(button.property("responsiveCompactWidth") or 38)
                button.setMaximumWidth(compact_width if compact else wide_width)
                button.setMinimumWidth(38 if compact else wide_min_width)
            else:
                button.setMaximumWidth(46 if compact else wide_width)
                button.setMinimumWidth(36 if compact else wide_min_width)
            policy = button.sizePolicy()
            if scope == "toolstrip" and compact:
                policy.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
            else:
                policy.setHorizontalPolicy(
                    QSizePolicy.Policy.Ignored if compact else QSizePolicy.Policy.Expanding
                )
            button.setSizePolicy(policy)
            text = str(button.property("fullText") or "")
            if text:
                self._set_responsive_button_text(button, text)

    def _update_header_responsive_mode(self) -> None:
        width = self.width()
        near_effective_minimum = width <= self.minimumSizeHint().width() + 24
        if width >= self._HEADER_COMPACT_EXIT_WIDTH:
            self._set_header_compact(False)
        elif width < self._HEADER_COMPACT_ENTER_WIDTH or near_effective_minimum:
            self._set_header_compact(True)

    @staticmethod
    def _compact_project_title(title: str, *, max_parts: int = 2, max_chars: int = 24) -> str:
        cleaned = title.strip()
        if not cleaned:
            return "Project"
        parts = [
            part
            for part in re.split(r"[\s_\-–—|:;,.()\[\]{}]+", cleaned)
            if part
        ]
        if not parts:
            return cleaned[:max_chars].rstrip()
        compact = " ".join(parts[:max_parts])
        if len(compact) > max_chars:
            compact = compact[:max_chars].rstrip()
        return compact or "Project"
