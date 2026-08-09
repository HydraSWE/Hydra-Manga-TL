"""Inspector construction and appearance controls for the workspace UI."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from hydra_manga_tl.core.region_types import normalize_region_type
from hydra_manga_tl.project.editor import RegionEdit
from hydra_manga_tl.ui.shared import CollapsibleSection, _speaker_icon, lucide_icon


class InspectorControllerMixin:
    """Own inspector widgets and region appearance conversion helpers."""

    def _build_inspector(self) -> QFrame:
        frame = QFrame(); frame.setObjectName("Inspector"); layout = QVBoxLayout(frame)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.setContentsMargins(10, 8, 10, 8); layout.setSpacing(6)
        tabs = QTabWidget(); layout.addWidget(tabs)
        text_tab = QWidget(); text_layout = QVBoxLayout(text_tab)
        text_layout.setContentsMargins(8, 7, 8, 8); text_layout.setSpacing(7)
        self.blocks = QListWidget(); self.blocks.setObjectName("TextBlocksList"); self.blocks.setWordWrap(True); self.blocks.setTextElideMode(Qt.TextElideMode.ElideRight); self.blocks.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); self.blocks.setMinimumHeight(140); self.blocks.setMaximumHeight(175); self.blocks.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); self.blocks.currentRowChanged.connect(self._select_block); self.blocks.itemSelectionChanged.connect(self._text_blocks_selection_changed)
        text_layout.addWidget(self.blocks)
        editor_host = QWidget(); editor_layout = QVBoxLayout(editor_host)
        editor_host.setMinimumWidth(0); editor_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        editor_layout.setContentsMargins(0, 0, 0, 0); editor_layout.setSpacing(7)
        form_host = QWidget(); form = QFormLayout(form_host); form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form_host.setMinimumWidth(0); form_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setContentsMargins(0, 0, 0, 0); form.setHorizontalSpacing(8); form.setVerticalSpacing(7)
        translation_field_min_width = 120
        self.original_text = QTextEdit(); self.original_text.setReadOnly(False); self.original_text.setFixedHeight(50)
        self.original_text.setToolTip("Correct OCR source text here; approval is separate from Apply & Rerender")
        self.original_text.setMinimumWidth(translation_field_min_width)
        self.original_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        original_host = QWidget(); original_host.setMinimumWidth(0)
        original_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        original_row = QHBoxLayout(original_host); original_row.setContentsMargins(0, 0, 0, 0); original_row.setSpacing(6)
        self.speak_original = QToolButton(); self.speak_original.setObjectName("SpeechButton"); self.speak_original.setIcon(_speaker_icon())
        self.speak_original.setIconSize(QSize(18, 18))
        self.speak_original.setToolTip("Play or stop the original text")
        self.speak_original.setFixedSize(30, 30)
        self.speak_original.setEnabled(False); self.speak_original.clicked.connect(self._speak_original)
        original_row.addWidget(self.original_text, 1); original_row.addWidget(self.speak_original)
        self.translation = QTextEdit(); self.translation.setFixedHeight(50)
        self.translation.setMinimumWidth(translation_field_min_width)
        self.translation.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.confidence = QLabel("—"); self.confidence.setObjectName("Muted")
        self.confidence.setMinimumWidth(0)
        self.confidence.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.confidence_bar = QProgressBar(); self.confidence_bar.setRange(0, 1000); self.confidence_bar.setTextVisible(False); self.confidence_bar.setFixedHeight(8)
        confidence_host = QWidget(); confidence_row = QHBoxLayout(confidence_host); confidence_row.setContentsMargins(0, 0, 0, 0); confidence_row.setSpacing(8)
        confidence_host.setMinimumWidth(0); confidence_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        confidence_row.addWidget(self.confidence, 1); confidence_row.addWidget(self.confidence_bar, 1)
        self.replace = QCheckBox("Replace source text"); self.replace.setChecked(True)
        self.font = QComboBox()
        font_options = [
            ("Arial", QFont("Arial")), ("Arial Bold", QFont("Arial", weight=QFont.Weight.Bold)),
            ("Comic Sans MS", QFont("Comic Sans MS")), ("Segoe UI", QFont("Segoe UI")),
        ]
        for label, sample_font in font_options:
            self.font.addItem(label); self.font.setItemData(self.font.count() - 1, sample_font, Qt.ItemDataRole.FontRole)
        self.font.currentTextChanged.connect(self._update_font_preview)
        self.font_size = QSpinBox(); self.font_size.setRange(0, 120); self.font_size.setSpecialValueText("Auto")
        self.alignment = QComboBox()
        for label, value in (("Left", "left"), ("Center", "center"), ("Right", "right")): self.alignment.addItem(label, value)
        self.alignment.setCurrentIndex(self.alignment.findData("center"))
        self.bubble_type = QComboBox()
        self._sync_bubble_type_options("dialogue")
        self.bubble_type.currentIndexChanged.connect(self._refresh_appearance_for_selected_type)
        self.color = QPushButton("#111111"); self.color.clicked.connect(self._choose_color)
        self.gradient_enabled = QCheckBox("Use gradient fill"); self.gradient_enabled.toggled.connect(self._update_gradient_controls_enabled)
        self.gradient_start = QPushButton("#111111"); self.gradient_start.clicked.connect(self._choose_gradient_start)
        self.gradient_end = QPushButton("#ffffff"); self.gradient_end.clicked.connect(self._choose_gradient_end)
        self.gradient_angle = QSpinBox(); self.gradient_angle.setRange(0, 180); self.gradient_angle.setSuffix(" deg"); self.gradient_angle.setSingleStep(15)
        self.offset_x = QSpinBox(); self.offset_x.setRange(-500, 500); self.offset_x.setSuffix(" px")
        self.offset_y = QSpinBox(); self.offset_y.setRange(-500, 500); self.offset_y.setSuffix(" px")
        self.offset_angle = QDoubleSpinBox(); self.offset_angle.setRange(-180.0, 180.0); self.offset_angle.setSuffix(" °"); self.offset_angle.setDecimals(1)
        self.offset_x.setSingleStep(5); self.offset_y.setSingleStep(5); self.offset_angle.setSingleStep(1.0)
        self.offset_x.setButtonSymbols(QSpinBox.ButtonSymbols.UpDownArrows)
        self.offset_y.setButtonSymbols(QSpinBox.ButtonSymbols.UpDownArrows)
        self.offset_angle.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.UpDownArrows)
        for widget in (
            original_host, confidence_host,
            self.font, self.font_size, self.alignment, self.bubble_type,
            self.color, self.gradient_enabled, self.gradient_start,
            self.gradient_end, self.gradient_angle, self.offset_x, self.offset_y, self.offset_angle,
        ):
            self._make_inspector_field_responsive(widget)
        self._make_inspector_field_responsive(self.translation, translation_field_min_width)
        for label, widget in (("Original", original_host), ("Translation", self.translation), ("Confidence", confidence_host)): form.addRow(label, widget)
        form.addRow(self.replace)
        editor_layout.addWidget(self._build_static_inspector_section("1. Translation", form_host))
        editor_layout.addWidget(self._build_inspector_section("2. Region", (("Region type", self.bubble_type), ("Alignment", self.alignment)), expanded=True))
        editor_layout.addWidget(self._build_inspector_section("3. Typography", (("Font", self.font), ("Size", self.font_size)), expanded=False))
        editor_layout.addWidget(self._build_inspector_section("4. Transform", (("X", self.offset_x), ("Y", self.offset_y), ("Rotation", self.offset_angle)), expanded=False))
        editor_layout.addWidget(self._build_inspector_section(
            "5. Appearance",
            (
                ("Color", self.color),
                ("Gradient", self.gradient_enabled),
                ("Start", self.gradient_start),
                ("End", self.gradient_end),
                ("Angle", self.gradient_angle),
            ),
            expanded=False,
        ))
        editor_layout.addStretch()
        form_scroll = QScrollArea(); form_scroll.setWidgetResizable(True); form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); form_scroll.setWidget(editor_host)
        text_layout.addWidget(form_scroll, 1)
        footer = QFrame(); footer.setObjectName("InspectorFooter")
        footer_layout = QVBoxLayout(footer); footer_layout.setContentsMargins(0, 7, 0, 0); footer_layout.setSpacing(6)
        actions = QGridLayout(); actions.setHorizontalSpacing(6); actions.setVerticalSpacing(6)
        self.remove_block = QPushButton("Remove Block"); self.remove_block.setObjectName("DangerButton"); self.remove_block.clicked.connect(self._remove_selected_block); self.remove_block.setEnabled(False)
        self.remove_block.setToolTip("Remove the selected automatic block, or delete the selected manual block")
        self.restore_auto = QPushButton("Restore Auto"); self.restore_auto.clicked.connect(self._restore_auto_blocks); self.restore_auto.setEnabled(False)
        self.restore_auto.setToolTip("Restore automatic blocks that were removed from this page")
        self.apply_button = QPushButton("Apply && Rerender"); self.apply_button.setObjectName("InspectorPrimary"); self.apply_button.clicked.connect(self._apply)
        self.apply_button.setToolTip("Save these text settings and rebuild the translated page")
        self.reset_button = QPushButton("Reset"); self.reset_button.clicked.connect(self._reset_edit)
        self.reset_button.setToolTip("Reset this block to its automatic text settings")
        self.approve_block = QPushButton("Approve Bubble"); self.approve_block.setToolTip("Approve this bubble for learning"); self.approve_block.clicked.connect(self._approve_ai_block)
        self.approve_page_bubbles = QPushButton("Approve Page OCR"); self.approve_page_bubbles.setToolTip("Approve all OCR/bubble issues on this page for learning"); self.approve_page_bubbles.clicked.connect(self._approve_ai_page_bubbles)
        self.approve_page_reviews = QPushButton("Approve Page Review"); self.approve_page_reviews.setToolTip("Approve all non-OCR review issues on this page for learning"); self.approve_page_reviews.clicked.connect(self._approve_ai_page_reviews)
        actions.addWidget(self.apply_button, 0, 0, 1, 2)
        actions.addWidget(self.remove_block, 1, 0); actions.addWidget(self.restore_auto, 1, 1)
        actions.addWidget(self.reset_button, 2, 0, 1, 2)
        actions.addWidget(self.approve_block, 3, 0, 1, 2)
        actions.addWidget(self.approve_page_bubbles, 4, 0); actions.addWidget(self.approve_page_reviews, 4, 1)
        footer_layout.addLayout(actions)
        text_layout.addWidget(footer)
        tabs.addTab(text_tab, "Text Blocks")
        self._update_font_preview(self.font.currentText()); self._update_color_swatch("#111111")
        self._update_gradient_start_swatch("#111111"); self._update_gradient_end_swatch("#ffffff")
        self._update_gradient_controls_enabled(False)
        info_tab = QWidget(); info_layout = QFormLayout(info_tab)
        self.info_path = QLabel("—"); self.info_path.setWordWrap(True); self.info_language = QLabel("—"); self.info_status = QLabel("—")
        info_layout.addRow("Source", self.info_path); info_layout.addRow("Language", self.info_language); info_layout.addRow("Status", self.info_status); tabs.addTab(info_tab, "Image Info")
        return frame

    def _build_static_inspector_section(self, title: str, content: QWidget) -> QFrame:
        section = QFrame(self)
        section.setObjectName("InspectorSection")
        section.setMinimumWidth(0)
        section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(section)
        layout.setContentsMargins(10, 7, 10, 8)
        layout.setSpacing(6)
        heading = QLabel(title)
        heading.setObjectName("InspectorSectionTitle")
        layout.addWidget(heading)
        layout.addWidget(content)
        return section

    @staticmethod
    def _make_inspector_field_responsive(widget: QWidget, minimum_width: int = 0) -> None:
        widget.setMinimumWidth(minimum_width)
        policy = widget.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
        widget.setSizePolicy(policy)

    def _build_inspector_section(self, title: str, rows: tuple[tuple[str, QWidget], ...], *, expanded: bool = False) -> CollapsibleSection:
        section = CollapsibleSection(title, expanded, self)
        section.setMinimumWidth(0)
        section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        section.body.setMinimumWidth(0)
        section.body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        form = QFormLayout(section.body)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setContentsMargins(10, 2, 10, 9)
        form.setHorizontalSpacing(8); form.setVerticalSpacing(7)
        for label, widget in rows:
            self._make_inspector_field_responsive(widget)
            form.addRow(label, widget)
        return section

    def _sync_bubble_type_options(self, current_type: str) -> None:
        current_type = "title" if current_type == "title" else normalize_region_type(current_type or "dialogue")
        self.bubble_type.blockSignals(True)
        self.bubble_type.clear()
        options = [
            ("Dialogue", "dialogue"),
            ("Title", "title"),
            ("SFX", "sfx"),
            ("Sign", "sign"),
            ("Credit", "credit"),
        ]
        for label, value in options:
            self.bubble_type.addItem(label, value)
        index = self.bubble_type.findData(current_type)
        self.bubble_type.setCurrentIndex(max(0, index))
        self.bubble_type.blockSignals(False)

    def _current_region_type(self) -> str:
        return normalize_region_type(self.bubble_type.currentData() or "dialogue")

    def _current_region_uses_art_appearance(self) -> bool:
        return self._current_region_type() in self._ART_APPEARANCE_TYPES

    def _refresh_appearance_for_selected_type(self) -> None:
        self._update_gradient_controls_enabled(self.gradient_enabled.isChecked())

    @staticmethod
    def _rgb_to_hex(value: object) -> str | None:
        if isinstance(value, str):
            color = QColor(value)
            return color.name() if color.isValid() else None
        if not isinstance(value, (list, tuple)) or len(value) < 3:
            return None
        try:
            red, green, blue = (max(0, min(255, int(round(float(item))))) for item in value[:3])
        except (TypeError, ValueError):
            return None
        return f"#{red:02x}{green:02x}{blue:02x}"

    @staticmethod
    def _hex_to_rgb(value: str) -> list[int]:
        color = QColor(value)
        if not color.isValid():
            color = QColor("#111111")
        return [color.red(), color.green(), color.blue()]

    @classmethod
    def _first_source_color(cls, group: dict) -> str | None:
        colors = group.get("source_text_colors")
        if not isinstance(colors, list):
            return None
        for color in colors:
            hex_color = cls._rgb_to_hex(color)
            if hex_color:
                return hex_color
        return None

    @classmethod
    def _profile_fill_color(cls, profile: dict | None) -> str | None:
        if not isinstance(profile, dict):
            return None
        fill = profile.get("fill")
        if not isinstance(fill, dict):
            return None
        return cls._rgb_to_hex(fill.get("dominant_color") or fill.get("average_color"))

    @classmethod
    def _profile_gradient_colors(cls, profile: dict | None) -> tuple[str, str, int] | None:
        if not isinstance(profile, dict):
            return None
        gradient = profile.get("gradient")
        if not isinstance(gradient, dict) or gradient.get("kind") != "linear":
            return None
        colors = gradient.get("colors")
        if not isinstance(colors, list) or len(colors) < 2:
            return None
        start = cls._rgb_to_hex(colors[0])
        end = cls._rgb_to_hex(colors[1])
        if not start or not end:
            return None
        try:
            angle = int(round(float(gradient.get("angle", 90))))
        except (TypeError, ValueError):
            angle = 90
        return start, end, max(0, min(180, angle))

    def _load_appearance_controls(self, group: dict, edit: RegionEdit) -> None:
        profile = edit.style_profile if isinstance(edit.style_profile, dict) else group.get("style_profile")
        color = (
            self._profile_fill_color(profile)
            or self._first_source_color(group)
            or self._rgb_to_hex(edit.color)
            or "#111111"
        )
        self._update_color_swatch(color)
        gradient = self._profile_gradient_colors(profile)
        if gradient is not None:
            start, end, angle = gradient
        else:
            start, end, angle = color, "#ffffff", 90
        self._update_gradient_start_swatch(start)
        self._update_gradient_end_swatch(end)
        self.gradient_angle.setValue(angle)
        self.gradient_enabled.blockSignals(True)
        self.gradient_enabled.setChecked(gradient is not None and self._current_region_uses_art_appearance())
        self.gradient_enabled.blockSignals(False)
        self._update_gradient_controls_enabled(self.gradient_enabled.isChecked())

    def _style_profile_from_appearance(self, group: dict, existing: RegionEdit) -> dict | None:
        if not self._current_region_uses_art_appearance():
            return None
        source = existing.style_profile if isinstance(existing.style_profile, dict) else group.get("style_profile")
        profile = dict(source) if isinstance(source, dict) else {}
        fill = dict(profile.get("fill")) if isinstance(profile.get("fill"), dict) else {}
        fill_color = self._hex_to_rgb(self.color.text())
        fill.update({
            "dominant_color": fill_color,
            "average_color": fill_color,
            "colors": [fill_color],
        })
        try:
            profile["version"] = max(2, int(profile.get("version", 2) or 2))
        except (TypeError, ValueError):
            profile["version"] = 2
        profile["fill"] = fill
        if self.gradient_enabled.isChecked():
            start = self._hex_to_rgb(self.gradient_start.text())
            end = self._hex_to_rgb(self.gradient_end.text())
            profile["gradient"] = {
                "kind": "linear",
                "colors": [start, end],
                "angle": float(self.gradient_angle.value()),
            }
        else:
            profile["gradient"] = None
        return profile

    def _build_offset_control(self, spinbox: QSpinBox, negative_label: str, positive_label: str) -> QWidget:
        host = QWidget()
        layout = QHBoxLayout(host); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(4)
        negative = QPushButton("-"); negative.setFixedWidth(30); negative.setToolTip(f"Nudge {negative_label}")
        positive = QPushButton("+"); positive.setFixedWidth(30); positive.setToolTip(f"Nudge {positive_label}")
        negative.clicked.connect(lambda: spinbox.setValue(spinbox.value() - spinbox.singleStep()))
        positive.clicked.connect(lambda: spinbox.setValue(spinbox.value() + spinbox.singleStep()))
        layout.addWidget(negative); layout.addWidget(spinbox, 1); layout.addWidget(positive)
        return host

    def _choose_color(self) -> None:
        color = QColorDialog.getColor(QColor(self.color.text()), self)
        if color.isValid(): self._update_color_swatch(color.name())

    def _choose_gradient_start(self) -> None:
        color = QColorDialog.getColor(QColor(self.gradient_start.text()), self)
        if color.isValid():
            self._update_gradient_start_swatch(color.name())

    def _choose_gradient_end(self) -> None:
        color = QColorDialog.getColor(QColor(self.gradient_end.text()), self)
        if color.isValid():
            self._update_gradient_end_swatch(color.name())

    def _set_color_swatch(self, button: QPushButton, value: str, tooltip: str) -> None:
        color = QColor(value)
        if not color.isValid(): color = QColor("#111111")
        pixmap = QPixmap(18, 18); pixmap.fill(color)
        button.setIcon(QIcon(pixmap)); button.setText(color.name())
        button.setToolTip(f"{tooltip} ({color.name()})")

    def _update_color_swatch(self, value: str) -> None:
        self._set_color_swatch(self.color, value, "Choose text color")

    def _update_gradient_start_swatch(self, value: str) -> None:
        self._set_color_swatch(self.gradient_start, value, "Choose gradient start color")

    def _update_gradient_end_swatch(self, value: str) -> None:
        self._set_color_swatch(self.gradient_end, value, "Choose gradient end color")

    def _update_gradient_controls_enabled(self, checked: bool = False) -> None:
        art_region = self._current_region_uses_art_appearance()
        self.gradient_enabled.setEnabled(art_region)
        self.gradient_start.setEnabled(art_region and checked)
        self.gradient_end.setEnabled(art_region and checked)
        self.gradient_angle.setEnabled(art_region and checked)
        if art_region:
            self.gradient_enabled.setToolTip("Use two colors for title, SFX, sign, or credit text")
        else:
            self.gradient_enabled.setToolTip("Gradient fill is available for title, SFX, sign, and credit regions")

    def _update_font_preview(self, family: str) -> None:
        if family == "Arial Bold":
            self.font.setFont(QFont("Arial", weight=QFont.Weight.Bold))
        else:
            self.font.setFont(QFont(family))

