"""Page editor, history, and text-block interaction behavior."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QKeySequenceEdit,
    QLineEdit,
    QListWidgetItem,
    QMessageBox,
    QTextEdit,
)

from hydra_manga_tl.core.ai_bridge import HYDRA_AI
from hydra_manga_tl.core.language import resolve_source_language
from hydra_manga_tl.core.settings import SETTINGS
from hydra_manga_tl.core.state import APP_STATE
from hydra_manga_tl.core.user_errors import hydra_ai_error, render_error, workspace_action_error
from hydra_manga_tl.project.editor import RegionEdit
from hydra_manga_tl.project.manual_region import normalize_image_rect
from hydra_manga_tl.project.workspace import WORKSPACE
from hydra_manga_tl.ui.dialogs import WorkingDialog
from hydra_manga_tl.ui.shared import TARGET_LANGUAGE_NAMES, _language_badge, _speaker_icon, confirm

from .constants import TRANSLATE_ELIGIBLE_STATUSES
from .editor_history import EditorHistoryMixin


class EditorControllerMixin(EditorHistoryMixin):
    """Own editor state, text-block selection, and undo/redo operations."""

    def _application_focus_changed(self, _old, _new) -> None:
        self._update_editor_shortcuts()

    def _manual_shortcut_sequence(self) -> QKeySequence:
        sequence = QKeySequence(SETTINGS.manual_textbox_shortcut or "Ctrl+D")
        return sequence if not sequence.isEmpty() else QKeySequence("Ctrl+D")

    def _title_reconstruction_shortcut_sequence(self) -> QKeySequence:
        sequence = QKeySequence(SETTINGS.title_reconstruction_shortcut or "Ctrl+F")
        return sequence if not sequence.isEmpty() else QKeySequence("Ctrl+F")

    def _configure_manual_shortcut(self) -> None:
        manual_sequence = self._manual_shortcut_sequence()
        if self._manual_shortcut is None:
            self._manual_shortcut = QShortcut(manual_sequence, self)
            self._manual_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self._manual_shortcut.activated.connect(self._cycle_manual_box_mode)
        else:
            self._manual_shortcut.setKey(manual_sequence)
        manual_label = manual_sequence.toString(QKeySequence.SequenceFormat.NativeText) or "Ctrl+D"
        self.add_box.setToolTip(f"Cycle Region Tool modes ({manual_label})")
        if hasattr(self, "title_reconstruction"):
            title_sequence = self._title_reconstruction_shortcut_sequence()
            if self._title_reconstruction_shortcut is None:
                self._title_reconstruction_shortcut = QShortcut(title_sequence, self)
                self._title_reconstruction_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                self._title_reconstruction_shortcut.activated.connect(self._cycle_title_reconstruction_mode)
            else:
                self._title_reconstruction_shortcut.setKey(title_sequence)
            title_label = title_sequence.toString(QKeySequence.SequenceFormat.NativeText) or "Ctrl+F"
            self.title_reconstruction.setToolTip(f"Cycle Title Reconstruction modes ({title_label})")

    def _register_editor_shortcut(self, sequence: str, callback, *, allow_text_focus: bool = False) -> QShortcut:
        shortcut = QShortcut(QKeySequence(sequence), self)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(callback)
        self._editor_shortcuts.append((shortcut, allow_text_focus))
        return shortcut

    def _configure_editor_shortcuts(self) -> None:
        self._register_editor_shortcut("R", lambda: self._begin_manual_box("rectangle"))
        self._register_editor_shortcut("P", lambda: self._begin_manual_box("polygon"))
        self._register_editor_shortcut("Ctrl+B", self._toggle_bubble_selector_shortcut)
        self._register_editor_shortcut("Ctrl+Return", self._apply, allow_text_focus=True)
        self._register_editor_shortcut("Ctrl+Backspace", self._reset_edit)
        self._register_editor_shortcut("Alt+O", self._next_ocr_issue, allow_text_focus=True)
        self._register_editor_shortcut("Alt+R", self._next_review_issue, allow_text_focus=True)
        self._register_editor_shortcut("F", self._fit_both)
        self._register_editor_shortcut("1", self._actual_both)
        self._register_editor_shortcut("Tab", lambda: self._select_relative_block(1))
        self._register_editor_shortcut("Backtab", lambda: self._select_relative_block(-1))
        self._register_editor_shortcut("Delete", self._delete_selected_context)
        self._register_editor_shortcut("Esc", self._cancel_manual_draw, allow_text_focus=True)
        self._update_editor_shortcuts()

    @staticmethod
    def _shortcut_text_focus() -> bool:
        focus = QApplication.focusWidget()
        return isinstance(focus, (QTextEdit, QLineEdit, QKeySequenceEdit, QComboBox))

    def _update_editor_shortcuts(self) -> None:
        pause_text_shortcuts = self._shortcut_text_focus()
        for shortcut, allow_text_focus in self._editor_shortcuts:
            shortcut.setEnabled(allow_text_focus or not pause_text_shortcuts)

    @staticmethod
    def _next_region_mode(mode: str) -> str:
        return "rectangle" if mode == "polygon" else "polygon"

    def _cycle_manual_box_mode(self) -> None:
        mode = "polygon" if self._region_cycle_mode == "polygon" else "rectangle"
        self._region_cycle_mode = self._next_region_mode(mode)
        self._begin_manual_box(mode)

    def _cycle_title_reconstruction_mode(self) -> None:
        mode = "polygon" if self._region_cycle_mode == "polygon" else "rectangle"
        self._region_cycle_mode = self._next_region_mode(mode)
        self._begin_manual_box(mode, kind="title")

    def _toggle_bubble_selector_shortcut(self) -> None:
        self.bubble_selector.setChecked(not self.bubble_selector.isChecked())
        self._toggle_bubble_selector()

    def _refresh_region_tool_style(self) -> None:
        for button in (self.add_box, getattr(self, "title_reconstruction", None)):
            if button is None:
                continue
            button.style().unpolish(button)
            button.style().polish(button)

    def _set_region_tool_active(self, mode: str, kind: str = "region") -> None:
        label = "Polygon" if mode == "polygon" else "Rectangle"
        if kind == "title":
            self._set_responsive_button_text(self.title_reconstruction, f"Title {label}")
            self.title_reconstruction.setChecked(True)
            self._set_responsive_button_text(self.add_box, "Region Tool")
            self.add_box.setChecked(False)
        else:
            self._set_responsive_button_text(self.add_box, label)
            self.add_box.setChecked(True)
            self._set_responsive_button_text(self.title_reconstruction, "Title Recon")
            self.title_reconstruction.setChecked(False)
        self._refresh_region_tool_style()

    def _reset_region_tool(self) -> None:
        self._set_responsive_button_text(self.add_box, "Region Tool")
        self.add_box.setChecked(False)
        self._set_responsive_button_text(self.title_reconstruction, "Title Recon")
        self.title_reconstruction.setChecked(False)
        if hasattr(self, "bubble_selector"):
            self.bubble_selector.setChecked(False)
        self._refresh_region_tool_style()

    def _load_image(self, index: int, block: int = -1) -> None:
        project = WORKSPACE.current
        if project is None or not (0 <= index < len(project.images)): return
        self.identity_tile.setChecked(False)
        self.canvas_stack.setCurrentWidget(self.page_canvases)
        self._sync_filmstrip_jump(index, len(project.images))
        image = project.images[index]; self.image_label.setText(f"Page {index + 1} of {len(project.images)}")
        self.info_path.setText(image.source_path); self.info_language.setText(image.source_language or "Not analyzed"); self.info_status.setText(image.status)
        self._update_canvas_status(image.status)
        self.original.set_badge(_language_badge("Original", image.source_language))
        target_name = TARGET_LANGUAGE_NAMES.get(project.target_language, project.target_language.upper())
        self.translated.set_badge(_language_badge("Translated", target_name))
        self._groups = []
        self.speech.stop(); self.speak_original.setEnabled(False)
        self.remove_block.setEnabled(False)
        self.restore_auto.setEnabled(bool(image.suppressed_auto_group_indices))
        if (image.translation_result and Path(image.translation_result).is_file()) or image.manual_regions:
            try: self._groups = WORKSPACE.effective_translation_payload(index)["translation_groups"]
            except (OSError, ValueError, json.JSONDecodeError): self._groups = []
        for group in self._groups:
            pending = self._pending_text_layouts.get((index, str(group.get("index"))))
            if pending is not None:
                group["text_layout"] = dict(pending)
        self.blocks.blockSignals(True); self.blocks.clear()
        for block_row, group in enumerate(self._groups, 1):
            ocr_reasons = WORKSPACE.ocr_review_reasons(group)
            item = QListWidgetItem(self._block_list_label(group, block_row, bool(ocr_reasons)))
            tooltip = group["original_text"]
            if ocr_reasons:
                tooltip = f"{tooltip}\nOCR review: {', '.join(ocr_reasons)}"
                item.setForeground(QColor("#ffcc66"))
            item.setToolTip(tooltip); self.blocks.addItem(item)
        self.blocks.setCurrentRow(block); self.blocks.blockSignals(False)
        self._update_ocr_queue_status()
        final = Path(image.rendered_image) if image.rendered_image else None
        self.original.set_content(Path(image.source_path), self._groups, block)
        self.translated.set_content(final, self._groups, block)
        if block >= 0: self._load_block(block)

    def _load_block(self, row: int) -> None:
        if not (0 <= row < len(self._groups)): return
        group = self._groups[row]; image = WORKSPACE.current.images[APP_STATE.selected_image]
        edit = image.edits.get(str(group["index"]), RegionEdit())
        self.original_text.setPlainText(group["original_text"]); self.translation.setPlainText(group["translated_text"])
        self._update_confidence_display(group)
        self.speak_original.setEnabled(bool(group.get("original_text")))
        self.remove_block.setEnabled(True)
        self.remove_block.setText("Delete Manual" if group.get("manual") else "Remove Auto")
        self.replace.setChecked(edit.replace); self.font.setCurrentText(edit.font_family); self.font_size.setValue(edit.font_size)
        alignment_index = self.alignment.findData(edit.alignment); self.alignment.setCurrentIndex(max(0, alignment_index))
        selected_type = edit.bubble_type or group.get("bubble_type", "dialogue")
        self._sync_bubble_type_options(str(selected_type))
        bubble_index = self.bubble_type.findData(selected_type); self.bubble_type.setCurrentIndex(max(0, bubble_index))
        self._load_appearance_controls(group, edit)
        self.offset_x.setValue(edit.offset_x); self.offset_y.setValue(edit.offset_y)
        self.offset_angle.setValue(edit.layout_angle or 0.0)
        self._update_ocr_queue_status()

    @staticmethod
    def _block_list_label(group: dict, row: int, has_ocr_review: bool) -> str:
        if group.get("bubble_type") == "title":
            kind = "Title Reconstruction"
        else:
            kind = "Manual Bubble" if group.get("manual") else ("OCR Bubble" if has_ocr_review else "Bubble")
        number = row if group.get("manual") else group.get("index", row)
        snippet = str(group.get("translated_text") or group.get("original_text") or "").strip().replace("\n", " ")
        if len(snippet) > 34:
            snippet = snippet[:31].rstrip() + "..."
        return f"{kind} {number}" + (f" • {snippet}" if snippet else "")

    def _update_confidence_display(self, group: dict) -> None:
        ocr = max(0.0, min(1.0, float(group.get("ocr_confidence", 0.0) or 0.0)))
        quality = str(group.get("translation_quality", "review" if group.get("review_reasons") else "good"))
        reasons = ", ".join(str(reason) for reason in group.get("review_reasons", [])[:2])
        label = f"OCR {ocr:.0%} • Translation {quality}"
        tooltip = label
        if reasons:
            tooltip += f" • {reasons}"
        self.confidence.setText(label)
        self.confidence.setToolTip(tooltip)
        self.confidence_bar.setValue(round(ocr * 1000))

    def _select_block(self, row: int) -> None:
        if row < 0: return
        modifiers = QApplication.keyboardModifiers()
        if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return
        APP_STATE.select(APP_STATE.selected_image, row); self.blocks.blockSignals(True); self.blocks.setCurrentRow(row); self.blocks.blockSignals(False)

    def _select_relative_block(self, delta: int) -> None:
        if not self._groups:
            return
        current = APP_STATE.selected_block
        if current < 0:
            current = self.blocks.currentRow()
        target = max(0, min(len(self._groups) - 1, current + delta))
        self._select_block(target)

    def _update_ocr_queue_status(self) -> None:
        count = len(WORKSPACE.ocr_review_queue())
        self._set_responsive_button_text(
            self.next_ocr_issue,
            f"Next OCR ({count})" if count else "Next OCR",
        )
        self.next_ocr_issue.setEnabled(count > 0 and not APP_STATE.busy)
        review_count = len(WORKSPACE.review_issue_queue())
        self._set_responsive_button_text(
            self.next_review_issue,
            f"Next Review ({review_count})" if review_count else "Next Review",
        )
        self.next_review_issue.setEnabled(review_count > 0 and not APP_STATE.busy)

    def _next_ocr_issue(self) -> None:
        queue = WORKSPACE.ocr_review_queue()
        if not queue:
            self.status.setText("No suspicious OCR blocks found in completed pages")
            self._update_ocr_queue_status()
            return
        current_image = APP_STATE.selected_image
        current_block = APP_STATE.selected_block
        selected = None
        for item in queue:
            if (item["image_index"], item["block_index"]) > (current_image, current_block):
                selected = item
                break
        selected = selected or queue[0]
        APP_STATE.select(int(selected["image_index"]), int(selected["block_index"]))
        self.status.setText(
            f"OCR review page {selected['page']}, block {selected['group_index']}: "
            + ", ".join(selected["reasons"][:3])
        )

    def _next_review_issue(self) -> None:
        queue = WORKSPACE.review_issue_queue()
        if not queue:
            self.status.setText("No review issues found in completed pages")
            self._update_ocr_queue_status()
            return
        current_image = APP_STATE.selected_image
        current_block = APP_STATE.selected_block
        selected = None
        for item in queue:
            if (item["image_index"], item["block_index"]) > (current_image, current_block):
                selected = item
                break
        selected = selected or queue[0]
        APP_STATE.select(int(selected["image_index"]), int(selected["block_index"]))
        self.status.setText(
            f"Review page {selected['page']}, block {selected['group_index']}: "
            + ", ".join(selected["reasons"][:3])
        )

    def _on_workspace_image_updated(self, index: int) -> None:
        if not WORKSPACE.current or not (0 <= index < len(WORKSPACE.current.images)):
            return
        image = WORKSPACE.current.images[index]
        item = self._filmstrip_items.get(image.id)
        if item is not None:
            self._update_filmstrip_item(item, image, index)
        if index == APP_STATE.selected_image:
            self._load_image(index)

    def _on_selection(self, image: int, block: int) -> None:
        if (
            image >= 0
            and self._ignore_next_open_page_selection > 0
            and self.identity_tile.isChecked()
            and self.canvas_stack.currentWidget() is self.identity_preview
        ):
            self._ignore_next_open_page_selection -= 1
            APP_STATE.selected_image = -1
            APP_STATE.selected_block = -1
            self._clear_filmstrip_current()
            self._show_identity_workspace()
            return
        if image >= 0:
            self._ignore_next_open_page_selection = 0
            if WORKSPACE.current is not None and WORKSPACE.current.selected_image != image:
                WORKSPACE.current.selected_image = image; WORKSPACE.save()
            self.filmstrip.blockSignals(True); self.filmstrip.setCurrentRow(image); self.filmstrip.blockSignals(False); self._load_image(image, block)

    def _set_quality(self, quality: str) -> None:
        if WORKSPACE.current is not None and WORKSPACE.current.quality != quality:
            WORKSPACE.current.quality = quality; WORKSPACE.save()

    def _set_source_language(self) -> None:
        if WORKSPACE.current is not None:
            WORKSPACE.current.source_language = self.source_combo.currentData(); WORKSPACE.save()

    def _set_text_style(self, style: str) -> None:
        if WORKSPACE.current is None or WORKSPACE.current.text_style == style:
            return
        WORKSPACE.current.text_style = style
        WORKSPACE.current.localization_style = style
        WORKSPACE.current.max_lines = 5 if style == "Novel" else 3
        WORKSPACE.save()

    def _show_working(self, title: str, message: str) -> WorkingDialog:
        dialog = WorkingDialog(title, message, self)
        dialog.show()
        QApplication.processEvents()
        return dialog

    def _close_working(self, dialog: WorkingDialog | None) -> None:
        if dialog is None:
            return
        dialog.accept()
        QApplication.processEvents()

    def _apply(self) -> None:
        selected_rows = set(APP_STATE.selected_blocks)
        if not selected_rows and (0 <= APP_STATE.selected_block < len(self._groups)):
            selected_rows = {APP_STATE.selected_block}
        valid_rows = [r for r in selected_rows if 0 <= r < len(self._groups)]
        if not valid_rows:
            return

        image_index = APP_STATE.selected_image
        before_state = self._capture_editor_history_state(image_index)
        working = self._show_working("Apply & Rerender", f"Preparing {len(valid_rows)} selected bubble(s)...")
        try:
            working.set_message(f"Saving edits for {len(valid_rows)} bubble(s)...")
            is_single = (len(valid_rows) == 1)
            edits_map: dict[int | str, RegionEdit] = {}
            for row in valid_rows:
                group = self._groups[row]
                grp_index = group["index"]
                existing = WORKSPACE.current.images[image_index].edits.get(str(grp_index), RegionEdit()) if WORKSPACE.current else RegionEdit()
                pending_layout = self._pending_text_layouts.get(self._layout_key(image_index, grp_index))

                orig_text = self.original_text.toPlainText() if is_single else group.get("original_text", "")
                trans_text = self.translation.toPlainText() if is_single else group.get("translated_text", "")

                edit = RegionEdit(
                    translated_text=trans_text,
                    replace=self.replace.isChecked(),
                    font_size=0 if pending_layout else self.font_size.value(),
                    offset_x=self.offset_x.value(),
                    offset_y=self.offset_y.value(),
                    font_family=self.font.currentText(),
                    color=self.color.text(),
                    alignment=self.alignment.currentData(),
                    original_text=orig_text,
                    bubble_type=self.bubble_type.currentData(),
                    style_profile=self._style_profile_from_appearance(group, existing),
                    layout_x=pending_layout["x"] if pending_layout else existing.layout_x,
                    layout_y=pending_layout["y"] if pending_layout else existing.layout_y,
                    layout_width=pending_layout["width"] if pending_layout else existing.layout_width,
                    layout_height=pending_layout["height"] if pending_layout else existing.layout_height,
                    layout_angle=self.offset_angle.value() if pending_layout is None and self.offset_angle.value() != (existing.layout_angle or 0.0) else (pending_layout.get("angle", existing.layout_angle or 0.0) if pending_layout else existing.layout_angle),
                )
                WORKSPACE.validate_edit(image_index, grp_index, edit)
                edits_map[grp_index] = edit
                self._pending_text_layouts.pop(self._layout_key(image_index, grp_index), None)

            WORKSPACE.update_edits_batch(image_index, edits_map)

            working.set_message("Rendering the translated page...")
            WORKSPACE.rerender_image(image_index, log_callback=working.append_log)
            working.set_message("Refreshing the editor preview...")
            primary_row = valid_rows[0]
            self._load_image(image_index, primary_row)
            self._discard_text_layout_history(image_index, {str(index) for index in edits_map})
            self._push_editor_history("Apply & Rerender", image_index, before_state)
            if len(valid_rows) > 1:
                self._finish_bubble_selection_session()
                self.status.setText(f"Applied region settings to {len(valid_rows)} text bubbles and rerendered page")
            else:
                self.status.setText("Text style applied and page rerendered")
        except (MemoryError, OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._close_working(working); working = None
            QMessageBox.warning(self, "Could not render", render_error(error))
        finally:
            self._close_working(working)

    def _approve_ai_block(self) -> None:
        row = APP_STATE.selected_block
        if not (0 <= row < len(self._groups)):
            return
        group = self._groups[row]
        if not confirm(
            self,
            "Approve Bubble",
            "Approve the selected bubble and record its review outcome for learning?",
        ):
            return
        working = self._show_working("Approve Bubble", "Capturing review outcome...")
        try:
            working.set_message("Sending bubble approval to Hydra AI...")
            summary = WORKSPACE.approve_ai_block(APP_STATE.selected_image, group["index"])
            if summary is None:
                self._close_working(working); working = None
                QMessageBox.warning(self, "Hydra AI", hydra_ai_error(HYDRA_AI.error))
                return
            working.set_message("Refreshing review queues...")
            self._update_ocr_queue_status()
            self.status.setText(f"Approved {summary.approved} learning sample(s); skipped {summary.skipped}")
        finally:
            self._close_working(working)
        QMessageBox.information(
            self,
            "Bubble approved",
            f"Bubble approval completed.\n\nApproved: {summary.approved}\nSkipped: {summary.skipped}",
        )

    def _approve_ai_page_bubbles(self) -> None:
        if WORKSPACE.current is None or APP_STATE.selected_image < 0:
            return
        page_items = [item for item in WORKSPACE.ocr_review_queue() if int(item["image_index"]) == APP_STATE.selected_image]
        if not page_items:
            self.status.setText("No OCR/bubble issues remain on this page")
            self._update_ocr_queue_status()
            return
        if not confirm(
            self, "Approve page OCR",
            f"Approve all OCR/bubble review items on this page ({len(page_items)} block(s))?\n\n"
            "Only captured corrections become training samples; unchanged outputs remain review outcomes.",
        ):
            return
        working = self._show_working("Approve Page OCR", "Capturing OCR/bubble review outcomes...")
        try:
            working.set_message("Sending OCR/bubble approvals to Hydra AI...")
            summary = WORKSPACE.approve_ai_page_bubbles(APP_STATE.selected_image)
            if summary is None:
                self._close_working(working); working = None
                QMessageBox.warning(self, "Hydra AI", hydra_ai_error(HYDRA_AI.error))
                return
            working.set_message("Refreshing review queues...")
            self._update_ocr_queue_status()
            self.status.setText(f"Approved {summary.approved} learning sample(s); skipped {summary.skipped}")
        finally:
            self._close_working(working)
        QMessageBox.information(
            self,
            "Page OCR approved",
            f"Bubble review approval completed.\n\nApproved: {summary.approved}\nSkipped: {summary.skipped}",
        )

    def _approve_ai_page_reviews(self) -> None:
        if WORKSPACE.current is None or APP_STATE.selected_image < 0:
            return
        page_items = [item for item in WORKSPACE.review_issue_queue() if int(item["image_index"]) == APP_STATE.selected_image]
        if not page_items:
            self.status.setText("No review issues remain on this page")
            self._update_ocr_queue_status()
            return
        if not confirm(
            self, "Approve page review",
            f"Approve all non-OCR review items on this page ({len(page_items)} block(s))?\n\n"
            "Only captured corrections become training samples; unchanged outputs remain review outcomes.",
        ):
            return
        working = self._show_working("Approve Page Review", "Capturing page review outcomes...")
        try:
            working.set_message("Sending review approvals to Hydra AI...")
            summary = WORKSPACE.approve_ai_page_reviews(APP_STATE.selected_image)
            if summary is None:
                self._close_working(working); working = None
                QMessageBox.warning(self, "Hydra AI", hydra_ai_error(HYDRA_AI.error))
                return
            working.set_message("Refreshing review queues...")
            self._update_ocr_queue_status()
            self.status.setText(f"Approved {summary.approved} learning sample(s); skipped {summary.skipped}")
        finally:
            self._close_working(working)
        QMessageBox.information(
            self,
            "Page review approved",
            f"Review approval completed.\n\nApproved: {summary.approved}\nSkipped: {summary.skipped}",
        )

    def _reset_edit(self) -> None:
            project = WORKSPACE.current
            row = APP_STATE.selected_block
            
            # Add bounds checking to prevent IndexError
            if project is None or row < 0 or row >= len(self._groups): 
                return
                
            group = self._groups[row]
            project.images[APP_STATE.selected_image].edits.pop(str(group["index"]), None)
            WORKSPACE.save()
            
            try: 
                WORKSPACE.rerender_image(APP_STATE.selected_image)
            except (OSError, ValueError): 
                pass
                
            self._load_image(APP_STATE.selected_image, row)

    def _delete_selected_context(self) -> None:
        focus = QApplication.focusWidget()
        if focus is self.filmstrip or (focus is not None and self.filmstrip.isAncestorOf(focus)):
            self._delete_selected_images()
            return
        self._remove_selected_block()

    def _delete_selected_manual_block(self) -> None:
        """Backward-compatible entry point for the editor Delete action."""
        self._remove_selected_block()

    def _select_pending_images(self) -> None:
        if not WORKSPACE.current:
            return
        self.filmstrip.blockSignals(True)
        self.filmstrip.clearSelection()
        for item in self._filmstrip_items.values():
            image_id = str(item.data(Qt.ItemDataRole.UserRole))
            image = next((img for img in WORKSPACE.current.images if img.id == image_id), None)
            if image and image.status in TRANSLATE_ELIGIBLE_STATUSES:
                item.setSelected(True)
        self.filmstrip.blockSignals(False)
        self._selection_changed()
        self.status.setText("Selected pending images for translation")

    def _clear_all_selections(self) -> None:
        self.filmstrip.blockSignals(True)
        self.filmstrip.clearSelection()
        self.filmstrip.blockSignals(False)
        self._selection_changed()

        APP_STATE.select(APP_STATE.selected_image, -1)
        self.original.update_region_highlights(set())
        self.translated.update_region_highlights(set())
        self.blocks.blockSignals(True)
        self.blocks.clearSelection()
        self.blocks.setCurrentRow(-1)
        self.blocks.blockSignals(False)
        self.remove_block.setText("Remove Block")
        self.status.setText("Selection cleared")

    def _toggle_bubble_selector(self) -> None:
        if self.bubble_selector.isChecked():
            self.add_box.setChecked(False)
            self.title_reconstruction.setChecked(False)
            self.original.begin_bubble_selection()
            self.status.setText("Bubble Selector active: Drag box or Ctrl/Shift-click to toggle bubbles")
        else:
            self.original.cancel_manual_selection()
            self.status.setText("Bubble Selector deactivated")

    def _finish_bubble_selection_session(self) -> None:
        APP_STATE.selected_block = -1
        APP_STATE.selected_blocks.clear()
        if self.bubble_selector.isChecked():
            self.bubble_selector.setChecked(False)
        self.original.cancel_manual_selection()
        self.original.update_region_highlights(set())
        self.translated.update_region_highlights(set())
        self.blocks.blockSignals(True)
        self.blocks.clearSelection()
        self.blocks.setCurrentRow(-1)
        self.blocks.blockSignals(False)
        self.remove_block.setText("Remove Block")
        self.remove_block.setEnabled(False)
        self.apply_button.setText("Apply && Rerender")

    def _batch_blocks_selected(self, primary_row: int, selected_rows: set[int]) -> None:
        APP_STATE.select(APP_STATE.selected_image, primary_row, selected_rows)
        self.original.update_region_highlights(selected_rows)
        self.translated.update_region_highlights(selected_rows)
        self.blocks.blockSignals(True)
        self.blocks.clearSelection()
        for row in selected_rows:
            if 0 <= row < self.blocks.count():
                item = self.blocks.item(row)
                if item:
                    item.setSelected(True)
        if 0 <= primary_row < self.blocks.count():
            self.blocks.setCurrentRow(primary_row)
        self.blocks.blockSignals(False)
        count = len(selected_rows)
        if count > 1:
            self.status.setText(f"{count} text bubbles selected on page")
            self.remove_block.setText(f"Remove Blocks ({count})")
            self.remove_block.setEnabled(True)
            self.apply_button.setText(f"Apply && Rerender ({count})")
        else:
            self.remove_block.setText("Remove Block")
            self.apply_button.setText("Apply && Rerender")

    def _text_blocks_selection_changed(self) -> None:
        selected_items = self.blocks.selectedItems()
        selected_rows = {self.blocks.row(item) for item in selected_items if item is not None}
        current_row = self.blocks.currentRow()
        if selected_rows:
            primary = current_row if current_row in selected_rows else next(iter(selected_rows))
            APP_STATE.select(APP_STATE.selected_image, primary, selected_rows)
            self.original.update_region_highlights(selected_rows)
            self.translated.update_region_highlights(selected_rows)
            if 0 <= primary < len(self._groups):
                self._load_block(primary)
            count = len(selected_rows)
            if count > 1:
                self.remove_block.setText(f"Remove Blocks ({count})")
                self.remove_block.setEnabled(True)
                self.apply_button.setText(f"Apply && Rerender ({count})")
            else:
                self.remove_block.setText("Remove Block")
                self.apply_button.setText("Apply && Rerender")
        else:
            APP_STATE.select(APP_STATE.selected_image, -1, set())
            self.original.update_region_highlights(set())
            self.translated.update_region_highlights(set())
            self.remove_block.setText("Remove Block")
            self.remove_block.setEnabled(False)
            self.apply_button.setText("Apply && Rerender")

    def _remove_selected_block(self) -> None:
        selected_rows = set(APP_STATE.selected_blocks)
        if not selected_rows and (0 <= APP_STATE.selected_block < len(self._groups)):
            selected_rows = {APP_STATE.selected_block}
        valid_rows = [r for r in selected_rows if 0 <= r < len(self._groups)]
        if not valid_rows:
            return
        if len(valid_rows) > 1:
            answer = QMessageBox.question(
                self,
                "Delete Multiple Bubbles",
                f"Delete the {len(valid_rows)} selected text bubbles?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            image_index = APP_STATE.selected_image
            before_state = self._capture_editor_history_state(image_index)
            working = self._show_working("Remove Selected Text Blocks", "Removing selected bubbles...")
            try:
                for row in sorted(valid_rows, reverse=True):
                    if 0 <= row < len(self._groups):
                        grp = self._groups[row]
                        if bool(grp.get("manual")):
                            WORKSPACE.delete_manual_region(image_index, str(grp["index"]))
                        else:
                            WORKSPACE.suppress_auto_region(image_index, int(grp["index"]))
                self._load_image(image_index, -1)
                self._push_editor_history("Delete Text Blocks", image_index, before_state)
                self.status.setText(f"{len(valid_rows)} text bubbles removed")
            finally:
                self._close_working(working)
            return

        row = valid_rows[0]
        group = self._groups[row]
        is_manual = bool(group.get("manual"))
        label = "manual bubble" if is_manual else "automatic bubble"
        answer = QMessageBox.question(
            self,
            "Delete Bubble",
            f"Delete the selected {label}?\n\n"
            + (
                "Covered automatic bubbles will be restored."
                if is_manual
                else "You can restore this bubble later with Restore Auto."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        success_message = ""
        image_index = APP_STATE.selected_image
        before_state = self._capture_editor_history_state(image_index)
        working = self._show_working("Remove Text Block", "Updating the translated page...")
        try:
            if is_manual:
                working.set_message("Deleting the manual block and restoring covered regions...")
                removed = WORKSPACE.delete_manual_region(image_index, str(group["index"]))
                message = "Manual text box deleted; its automatic blocks were restored"
            else:
                working.set_message("Removing the automatic block...")
                removed = WORKSPACE.suppress_auto_region(image_index, int(group["index"]))
                message = "Automatic block removed; draw an Add Text Box replacement if needed"
            if removed:
                working.set_message("Refreshing the editor preview...")
                self._load_image(image_index, max(-1, row - 1))
                self._push_editor_history("Delete Text Block", image_index, before_state)
                self.status.setText(message)
                success_message = message
        except (MemoryError, OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._close_working(working); working = None
            QMessageBox.warning(self, "Could not remove text block", render_error(error))
        finally:
            self._close_working(working)
        if success_message:
            QMessageBox.information(self, "Bubble deleted", success_message)

    def _restore_auto_blocks(self) -> None:
        image_index = APP_STATE.selected_image
        before_state = self._capture_editor_history_state(image_index)
        try:
            if WORKSPACE.restore_auto_regions(image_index):
                self._load_image(image_index)
                self._push_editor_history("Restore Auto Blocks", image_index, before_state)
                self.status.setText("Removed automatic blocks restored")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            QMessageBox.warning(self, "Could not restore automatic blocks", render_error(error))

    def _speak_original(self) -> None:
        if not (0 <= APP_STATE.selected_block < len(self._groups)):
            return
        group = self._groups[APP_STATE.selected_block]
        language = group.get("source_language")
        if WORKSPACE.current is not None:
            image_language = WORKSPACE.current.images[APP_STATE.selected_image].source_language
            language = resolve_source_language(WORKSPACE.current.source_language, image_language, language)
        self.speech.speak(str(group.get("original_text", "")), str(language or ""))

