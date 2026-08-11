"""Filmstrip, page selection, and thumbnail behavior for the workspace UI."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QSize, Qt, QThread, QTimer
from PySide6.QtGui import QColor, QCursor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QListWidgetItem,
    QMenu,
    QMessageBox,
)

from hydra_manga_tl.core.settings import SETTINGS
from hydra_manga_tl.core.state import APP_STATE
from hydra_manga_tl.core.user_errors import pipeline_error, workspace_action_error
from hydra_manga_tl.project.import_scan import ThumbnailWorker
from hydra_manga_tl.project.workspace import WORKSPACE
from hydra_manga_tl.ui.dialogs import IdentityPreviewDialog
from hydra_manga_tl.ui.shared import (
    FILMSTRIP_CARD_SIZE,
    FILMSTRIP_PREVIEW_SIZE,
    _page_label,
    confirm,
    lucide_icon,
)

from .constants import TRANSLATE_ELIGIBLE_STATUSES


class FilmstripControllerMixin:
    """Own filmstrip interactions while relying on widgets built by WorkspaceScreen."""

    def _filmstrip_expanded_changed(self, expanded: bool) -> None:
        if hasattr(self, "filmstrip_jump_host"):
            self.filmstrip_jump_host.setVisible(expanded)
        project = WORKSPACE.current
        if self._filmstrip_collapse_mode() == "always_collapsed":
            return
        if project is None or bool(getattr(project, "filmstrip_visible", True)) == expanded:
            return
        project.filmstrip_visible = expanded
        WORKSPACE.save()

    @staticmethod
    def _filmstrip_collapse_mode() -> str:
        mode = getattr(SETTINGS, "filmstrip_collapse_mode", "current") or "current"
        if mode in {"always_collapsed", "always_expanded"}:
            return mode
        return "current"

    def _apply_filmstrip_collapse_preference(self, project, project_id: str) -> None:
        mode = self._filmstrip_collapse_mode()
        project_changed = project_id != self._filmstrip_policy_project_id
        mode_changed = mode != self._filmstrip_policy_mode
        if mode == "always_collapsed":
            if project_changed or mode_changed:
                self.filmstrip_section.set_expanded(False)
                self.filmstrip_jump_host.setVisible(False)
        elif mode == "always_expanded":
            if project_changed or mode_changed:
                self.filmstrip_section.set_expanded(True)
                self.filmstrip_jump_host.setVisible(True)
        else:
            # "current" — respect the per-project filmstrip_visible flag
            expanded = bool(getattr(project, "filmstrip_visible", True))
            self.filmstrip_section.set_expanded(expanded)
            self.filmstrip_jump_host.setVisible(expanded)
        self._filmstrip_policy_project_id = project_id
        self._filmstrip_policy_mode = mode

    def _show_identity_preview(self) -> None:
        if self.identity_thumbnail_path is None:
            return
        IdentityPreviewDialog(self.identity_thumbnail_path, self).exec()
        if self.filmstrip.currentRow() >= 0:
            self.filmstrip.setFocus()

    def _identity_tile_menu(self, position) -> None:
        if self.identity_thumbnail_path is None:
            return
        menu = QMenu(self)
        set_thumbnail = menu.addAction("Set as Recent Thumbnail")
        set_thumbnail.setIcon(lucide_icon("image-plus"))
        set_thumbnail.setEnabled(WORKSPACE.current is not None and not APP_STATE.busy)
        set_thumbnail.triggered.connect(self._set_identity_recent_thumbnail)
        menu.exec(self.identity_tile.mapToGlobal(position))

    def _reset_project_view_state(self) -> None:
        self.stop_thumbnail_loading()
        self._filmstrip_build_generation += 1
        self._filmstrip_project_id = ""
        self._filmstrip_items = {}
        self._ignore_next_open_page_selection = False
        self.identity_tile.setChecked(False)
        self.filmstrip.blockSignals(True)
        self.filmstrip.clearSelection()
        self.filmstrip.clear()
        self.filmstrip.blockSignals(False)
        self.filmstrip_jump.clear()
        self.filmstrip_jump.setEnabled(False)
        self.filmstrip_jump_button.setEnabled(False)
        self._groups = []
        if hasattr(self, "canvas_stack"):
            self.canvas_stack.setCurrentWidget(self.page_canvases)
        if hasattr(self, "original_status"):
            self._update_canvas_status("ready")

    def _identity_workspace_active(self) -> bool:
        return (
            self.identity_thumbnail_path is not None
            and self.identity_tile.isChecked()
            and self.filmstrip.currentRow() < 0
            and self.canvas_stack.currentWidget() is self.identity_preview
        )

    def _show_identity_tile_in_filmstrip(self) -> None:
        self.filmstrip.horizontalScrollBar().setValue(0)
        self.identity_tile.raise_()
        QTimer.singleShot(0, lambda: self.filmstrip.horizontalScrollBar().setValue(0))

    def _select_identity(self) -> None:
        if self.identity_thumbnail_path is None:
            return
        self.identity_tile.setChecked(True)
        self.filmstrip.blockSignals(True)
        self._clear_filmstrip_current()
        self.filmstrip.blockSignals(False)
        self._selection_changed()
        if APP_STATE.selected_image != -1 or APP_STATE.selected_block != -1:
            APP_STATE.select(-1, -1)
        # Absorb 2 incoming selection_changed signals:
        # 1) set_project's own selection_changed.emit at end of set_project()
        # 2) _set_current's APP_STATE.select(last_page) call after set_project returns
        self._ignore_next_open_page_selection = 2
        self._show_identity_workspace()
        self._show_identity_tile_in_filmstrip()

    def _show_identity_workspace(self) -> None:
        if self.identity_thumbnail_path is None:
            return
        self.image_label.setText("Hydra identity")
        self.selection_label.setText("Hydra selected")
        self.info_path.setText(str(self.identity_thumbnail_path))
        self.info_language.setText("Brand identity")
        self.info_status.setText("Preview")
        self.original.set_badge("Hydra Identity")
        self.translated.set_badge("Hydra Identity")
        self.identity_preview.set_badge("Hydra Identity")
        self._update_canvas_status("Preview")
        self.canvas_stack.setCurrentWidget(self.identity_preview)
        self._groups = []
        self.speech.stop()
        self.speak_original.setEnabled(False)
        self.remove_block.setEnabled(False)
        self.restore_auto.setEnabled(False)
        self.add_box.setEnabled(False)
        self.title_reconstruction.setEnabled(False)
        self.blocks.blockSignals(True)
        self.blocks.clear()
        self.blocks.blockSignals(False)
        self.original_text.clear()
        self.translation.clear()
        self.confidence.setText("—")
        self.confidence_bar.setValue(0)
        self.original.set_content(None, [], -1)
        self.translated.set_content(None, [], -1)
        self.identity_preview.set_content(self.identity_thumbnail_path, [], -1)
        # Defer fit so Qt has finalized canvas_stack geometry after setCurrentWidget
        QTimer.singleShot(0, self.identity_preview.fit_image)

    def _clear_filmstrip_current(self) -> None:
        self.filmstrip.clearSelection()
        self.filmstrip.setCurrentIndex(QModelIndex())
        selection_model = self.filmstrip.selectionModel()
        if selection_model is not None:
            selection_model.clearCurrentIndex()

    @staticmethod
    def _image_id(image) -> str:
        return str(getattr(image, "id", image.source_path))

    def _rebuild_filmstrip(self, project, project_id: str, image_ids: list[str], current: int) -> None:
        selected_ids = {
            str(item.data(Qt.ItemDataRole.UserRole)) for item in self.filmstrip.selectedItems()
        } if project_id == self._filmstrip_project_id else set()
        self.stop_thumbnail_loading()
        self._filmstrip_build_generation += 1
        generation = self._filmstrip_build_generation
        self.filmstrip.blockSignals(True); self.filmstrip.clear(); self._filmstrip_items = {}
        thumbnail_inputs: list[tuple[str, str]] = []
        chunk_size = max(1, int(getattr(self, "_filmstrip_build_chunk_size", 24) or 24))

        def append_page_item(image_index: int) -> None:
            image = project.images[image_index]
            image_id = image_ids[image_index]
            item = QListWidgetItem(); item.setData(Qt.ItemDataRole.UserRole, image_id)
            item.setSizeHint(FILMSTRIP_CARD_SIZE); item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setIcon(self._thumbnail_icon())
            self._update_filmstrip_item(item, image, image_index)
            self.filmstrip.addItem(item); self._filmstrip_items[image_id] = item
            if Path(image.source_path).is_file():
                thumbnail_inputs.append((image_id, image.source_path))

        def finish() -> None:
            if generation != self._filmstrip_build_generation:
                return
            self._append_add_pages_item()
            self._filmstrip_project_id = project_id
            if current >= 0:
                self.filmstrip.setCurrentRow(current)
                self.filmstrip.clearSelection()
            for image_id in selected_ids:
                if image_id in self._filmstrip_items:
                    self._filmstrip_items[image_id].setSelected(True)
            self.filmstrip.blockSignals(False); self._selection_changed()
            if thumbnail_inputs:
                self._queue_thumbnail_loading(project_id, thumbnail_inputs)

        def build_chunk(start: int) -> None:
            if generation != self._filmstrip_build_generation:
                return
            stop = min(len(project.images), start + chunk_size)
            for image_index in range(start, stop):
                append_page_item(image_index)
            if stop < len(project.images):
                QTimer.singleShot(0, lambda next_start=stop: build_chunk(next_start))
                return
            finish()

        if len(project.images) > chunk_size:
            build_chunk(0)
            return
        for image_index in range(len(project.images)):
            append_page_item(image_index)
        finish()

    def _append_add_pages_item(self) -> None:
        add_item = QListWidgetItem()
        add_item.setData(Qt.ItemDataRole.UserRole, "__add_pages__")
        add_item.setSizeHint(FILMSTRIP_CARD_SIZE)
        add_item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
        add_item.setIcon(self.filmstrip.create_add_pages_icon())
        add_item.setText("Add Pages")
        add_item.setToolTip("Click to import additional manga pages or folders into this project")
        add_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.filmstrip.addItem(add_item)

    def _current_filmstrip_items(self) -> dict[str, QListWidgetItem]:
        return {
            str(self.filmstrip.item(row).data(Qt.ItemDataRole.UserRole)): self.filmstrip.item(row)
            for row in range(self.filmstrip.count())
            if self.filmstrip.item(row) is not None and str(self.filmstrip.item(row).data(Qt.ItemDataRole.UserRole) or "") != "__add_pages__"
        }

    def _on_add_pages_clicked(self) -> None:
        if APP_STATE.busy:
            QMessageBox.information(
                self,
                "Translation Active",
                "Please wait for active translation tasks to finish before adding new pages."
            )
            return
        menu = QMenu(self)
        action_images = menu.addAction("Add Image File(s)...")
        action_folder = menu.addAction("Add Image Folder...")
        chosen = menu.exec(QCursor.pos())
        from hydra_manga_tl.ui.landing import configured_manga_import_root
        if chosen == action_images:
            files, _ = QFileDialog.getOpenFileNames(
                self,
                "Add manga images",
                "",
                "Images (*.jpg *.jpeg *.png *.webp *.tif *.tiff *.bmp)"
            )
            if files:
                self._append_input_paths([Path(p) for p in files])
        elif chosen == action_folder:
            folder = QFileDialog.getExistingDirectory(
                self,
                "Add manga folder",
                str(configured_manga_import_root()),
                QFileDialog.Option.ShowDirsOnly,
            )
            if folder:
                self._append_input_paths([Path(folder)])

    def _append_input_paths(self, paths: list[Path]) -> None:
        if not paths or WORKSPACE.current is None:
            return
        added = WORKSPACE.add_inputs(paths)
        if added > 0:
            self.status.setText(f"Added {added} page{'s' if added != 1 else ''} to project.")

    @staticmethod
    def _update_filmstrip_item(item: QListWidgetItem, image, image_index: int) -> None:
        item.setText(_page_label(image.source_path, image_index))
        item.setForeground(QColor({"ready":"#66d69a", "review":"#ffcc66", "failed":"#ff6b73", "ocr":"#69a0ff", "translating":"#69a0ff", "reconstructing":"#69a0ff"}.get(image.status, "#d7deea")))
        details = f"{Path(image.source_path).name}\n{image.source_path}\nStatus: {image.status}"
        if image.error: details += f"\n{pipeline_error(image.error)}"
        item.setToolTip(details)

    def _start_thumbnail_loading(self, project_id: str, images: list[tuple[str, str]]) -> None:
        thread = QThread(self); worker = ThumbnailWorker(images, QSize(72, 78)); worker.moveToThread(thread)
        job = (thread, worker); self._thumbnail_jobs.append(job)
        thread.started.connect(worker.run)
        worker.thumbnail_ready.connect(lambda image_id, image, pid=project_id: self._apply_thumbnail(pid, image_id, image))
        worker.progress.connect(lambda current, total, name, pid=project_id: self._thumbnail_progress(pid, current, total, name))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda job=job: self._thumbnail_finished(job))
        thread.start()

    def _queue_thumbnail_loading(self, project_id: str, images: list[tuple[str, str]]) -> None:
        self._start_thumbnail_loading(project_id, images)

    def _apply_thumbnail(self, project_id: str, image_id: str, image) -> None:
        if project_id != self._filmstrip_project_id:
            return
        self._filmstrip_items = self._current_filmstrip_items()
        item = self._filmstrip_items.get(image_id)
        if item is None:
            return
        item.setIcon(self._thumbnail_icon(image))

    @staticmethod
    def _thumbnail_icon(image=None) -> QIcon:
        canvas = QPixmap(FILMSTRIP_PREVIEW_SIZE)
        canvas.fill(QColor("#0b1017"))
        painter = QPainter(canvas); painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setPen(QPen(QColor("#2a3749"), 1)); painter.drawRect(0, 0, canvas.width() - 1, canvas.height() - 1)
        if image is not None and not image.isNull():
            source = QPixmap.fromImage(image)
            target = source.scaled(
                canvas.width() - 4, canvas.height() - 4,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            )
            x = (canvas.width() - target.width()) // 2; y = (canvas.height() - target.height()) // 2
            painter.drawPixmap(x, y, target)
        painter.end()
        return QIcon(canvas)

    def _thumbnail_progress(self, project_id: str, current: int, total: int, name: str) -> None:
        if project_id == self._filmstrip_project_id and not APP_STATE.busy:
            count = len(WORKSPACE.current.images) if WORKSPACE.current is not None else total
            self.count_label.setText(f"{count} images • previews {current}/{total}")

    def _thumbnail_finished(self, job: tuple[QThread, ThumbnailWorker]) -> None:
        if job in self._thumbnail_jobs:
            self._thumbnail_jobs.remove(job)
        job[0].deleteLater()
        if not self._thumbnail_jobs and not APP_STATE.busy:
            count = len(WORKSPACE.current.images) if WORKSPACE.current is not None else 0
            self.count_label.setText(f"{count} images")

    def stop_thumbnail_loading(self) -> None:
        for thread, _ in list(self._thumbnail_jobs):
            thread.requestInterruption(); thread.quit(); thread.wait(3000)

    def _filmstrip_current_changed(self, row: int) -> None:
        if row >= 0:
            item = self.filmstrip.item(row)
            image_id = str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""
            if image_id == "__add_pages__":
                return
            self._ignore_next_open_page_selection = 0
            self.identity_tile.setChecked(False)
            APP_STATE.select(row)

    def _sync_filmstrip_jump(self, index: int, total: int | None = None) -> None:
        if not hasattr(self, "filmstrip_jump"):
            return
        page_count = total
        if page_count is None:
            page_count = len(WORKSPACE.current.images) if WORKSPACE.current is not None else 0
        enabled = page_count > 0
        self.filmstrip_jump.setEnabled(enabled)
        self.filmstrip_jump_button.setEnabled(enabled)
        self.filmstrip_jump.setText(str(index + 1) if enabled and 0 <= index < page_count else "")

    def _jump_to_filmstrip_page(self) -> None:
        project = WORKSPACE.current
        text = self.filmstrip_jump.text().strip()
        if project is None or not project.images:
            self.status.setText("No pages are available.")
            return
        if not text:
            self.status.setText("Enter a page number to jump.")
            return
        try:
            page_number = int(text)
        except ValueError:
            self.status.setText("Enter a valid page number.")
            return
        if not (1 <= page_number <= len(project.images)):
            self.status.setText(f"Page {page_number} is not available.")
            return
        index = page_number - 1
        self.identity_tile.setChecked(False)
        APP_STATE.select(index, -1)
        item = self.filmstrip.item(index)
        if item is not None:
            self.filmstrip.scrollToItem(item, QAbstractItemView.ScrollHint.EnsureVisible)
        self.status.setText(f"Jumped to page {page_number}.")

    def _on_filmstrip_reordered(self, ordered_ids: list[str]) -> None:
        selected_ids = {
            str(item.data(Qt.ItemDataRole.UserRole)) for item in self.filmstrip.selectedItems()
            if str(item.data(Qt.ItemDataRole.UserRole) or "") != "__add_pages__"
        } | set(getattr(self.filmstrip, "_last_moved_ids", set()))
        before_ids = [image.id for image in WORKSPACE.current.images] if WORKSPACE.current is not None else []
        moved_count = len(selected_ids) or 1
        moved_id = next((image_id for image_id in ordered_ids if image_id in selected_ids), ordered_ids[0] if ordered_ids else "")
        from_position = before_ids.index(moved_id) + 1 if moved_id in before_ids else 0
        to_position = ordered_ids.index(moved_id) + 1 if moved_id in ordered_ids else 0
        changed_ids = {
            image_id for index, image_id in enumerate(ordered_ids)
            if index >= len(before_ids) or before_ids[index] != image_id
        }
        self._filmstrip_items = self._current_filmstrip_items()
        if not WORKSPACE.reorder_images(ordered_ids):
            self._filmstrip_project_id = ""
            if WORKSPACE.current is not None:
                self.refresh(WORKSPACE.current)
            return
        if before_ids != ordered_ids:
            self._filmstrip_undo.append({
                "kind": "filmstrip_reorder",
                "before": list(before_ids),
                "after": list(ordered_ids),
                "selected_ids": sorted(selected_ids),
                "moved_count": moved_count,
            })
            del self._filmstrip_undo[:-200]
            self._filmstrip_redo.clear()
        if WORKSPACE.current is not None:
            self.refresh(WORKSPACE.current, force_filmstrip_rebuild=True)
        self.filmstrip.blockSignals(True); self.filmstrip.clearSelection()
        first_selected = next((self._filmstrip_items[image_id] for image_id in ordered_ids if image_id in selected_ids and image_id in self._filmstrip_items), None)
        if first_selected is not None:
            self.filmstrip.setCurrentItem(first_selected)
            self.filmstrip.scrollToItem(first_selected, QAbstractItemView.ScrollHint.PositionAtCenter)
        for image_id in selected_ids:
            if image_id in self._filmstrip_items:
                self._filmstrip_items[image_id].setSelected(True)
        self.filmstrip.blockSignals(False); self._selection_changed()
        self._flash_filmstrip_items(changed_ids or selected_ids)
        if from_position and to_position:
            message = (
                f"Page moved: Page {from_position} -> Position {to_position}"
                if moved_count == 1 else f"Moved {moved_count} pages -> Position {to_position}"
            )
            self.status.setText(message)
            QTimer.singleShot(2500, lambda text=message: self.status.setText("Ready") if self.status.text() == text else None)

    def _focus_in_filmstrip(self) -> bool:
        focus = QApplication.focusWidget()
        return focus is self.filmstrip or (
            focus is not None and self.filmstrip.isAncestorOf(focus)
        )

    def _apply_filmstrip_history_command(self, command: dict, state_key: str) -> bool:
        if command.get("kind") != "filmstrip_reorder" or WORKSPACE.current is None:
            return False
        ordered_ids = [str(image_id) for image_id in command.get(state_key, [])]
        current_ids = [image.id for image in WORKSPACE.current.images]
        if set(ordered_ids) != set(current_ids) or len(ordered_ids) != len(current_ids):
            self.status.setText("Filmstrip history no longer matches this project")
            return False
        if ordered_ids == current_ids:
            return True
        if not WORKSPACE.reorder_images(ordered_ids):
            self.status.setText("Could not restore filmstrip order")
            return False
        self.refresh(WORKSPACE.current, force_filmstrip_rebuild=True)
        selected_ids = {
            str(image_id)
            for image_id in command.get("selected_ids", [])
            if str(image_id) in self._filmstrip_items
        }
        self.filmstrip.blockSignals(True)
        self.filmstrip.clearSelection()
        first_selected = None
        for image_id in ordered_ids:
            item = self._filmstrip_items.get(image_id)
            if item is None:
                continue
            if image_id in selected_ids:
                item.setSelected(True)
                first_selected = first_selected or item
        if first_selected is not None:
            self.filmstrip.setCurrentItem(first_selected)
            self.filmstrip.scrollToItem(first_selected, QAbstractItemView.ScrollHint.PositionAtCenter)
        self.filmstrip.blockSignals(False)
        self._selection_changed()
        self._flash_filmstrip_items(selected_ids)
        action = "Undid" if state_key == "before" else "Redid"
        count = int(command.get("moved_count", len(selected_ids) or 1) or 1)
        self.status.setText(f"{action} filmstrip reorder ({count} page{'s' if count != 1 else ''})")
        return True

    def _undo_workspace(self) -> None:
        if self._focus_in_filmstrip():
            if not self._filmstrip_undo:
                return
            command = self._filmstrip_undo.pop()
            if self._apply_filmstrip_history_command(command, "before"):
                self._filmstrip_redo.append(command)
            return
        self._undo_text_layout()

    def _redo_workspace(self) -> None:
        if self._focus_in_filmstrip():
            if not self._filmstrip_redo:
                return
            command = self._filmstrip_redo.pop()
            if self._apply_filmstrip_history_command(command, "after"):
                self._filmstrip_undo.append(command)
            return
        self._redo_text_layout()

    def _flash_filmstrip_items(self, image_ids: set[str]) -> None:
        if not image_ids:
            return
        original_backgrounds = {}
        for image_id in image_ids:
            item = self._filmstrip_items.get(image_id)
            if item is None:
                continue
            original_backgrounds[image_id] = item.background()
            item.setBackground(QColor("#24486f"))
        QTimer.singleShot(420, lambda: self._restore_filmstrip_backgrounds(original_backgrounds))

    def _restore_filmstrip_backgrounds(self, backgrounds) -> None:
        for image_id, background in backgrounds.items():
            item = self._filmstrip_items.get(image_id)
            if item is not None:
                item.setBackground(background)

    def _selection_changed(self) -> None:
        count = len(self.filmstrip.selectedItems())
        self.selection_label.setText(f"{count} selected")
        eligible = self._selected_image_ids(eligible_only=True)
        self._set_responsive_button_text(
            self.selected_button,
            f"Translate Selected ({len(eligible)})" if eligible else "Translate Selected",
        )
        self.selected_button.setEnabled(bool(eligible) and not APP_STATE.busy)
        if hasattr(self, "add_box"):
            self.add_box.setEnabled(not self._manual_busy)
        if hasattr(self, "title_reconstruction"):
            self.title_reconstruction.setEnabled(not self._manual_busy)

    def _filmstrip_menu(self, position) -> None:
        item = self.filmstrip.itemAt(position)
        if item is None:
            return
        if not item.isSelected():
            self.filmstrip.clearSelection(); item.setSelected(True); self.filmstrip.setCurrentItem(item)
        selected_ids = self._selected_image_ids(eligible_only=True)
        menu = QMenu(self)
        translate = menu.addAction(f"Translate Selected ({len(selected_ids)})")
        translate.setEnabled(bool(selected_ids) and not APP_STATE.busy)
        translate.triggered.connect(lambda: self._translate_selected(selected_ids))
        all_selected = self._selected_image_ids(eligible_only=False)
        retranslate = menu.addAction(f"Retranslate Selected ({len(all_selected)})")
        retranslate.setEnabled(bool(all_selected) and not APP_STATE.busy)
        retranslate.triggered.connect(lambda: self._retranslate_selected(all_selected))
        menu.addSeparator()
        thumbnail_image_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        set_thumbnail = menu.addAction("Set as Recent Thumbnail")
        set_thumbnail.setIcon(lucide_icon("image-plus"))
        set_thumbnail.setEnabled(bool(thumbnail_image_id) and WORKSPACE.current is not None and not APP_STATE.busy)
        set_thumbnail.triggered.connect(lambda: self._set_recent_thumbnail(thumbnail_image_id))
        menu.addSeparator()
        delete_label = "Delete Image" if len(all_selected) == 1 else f"Delete Images ({len(all_selected)})"
        delete_pages = menu.addAction(delete_label)
        delete_pages.setEnabled(bool(all_selected) and not APP_STATE.busy)
        delete_pages.triggered.connect(lambda: self._delete_selected_images(all_selected))
        menu.exec(self.filmstrip.viewport().mapToGlobal(position))

    def _selected_image_ids(self, eligible_only: bool = False) -> set[str]:
        selected = {str(item.data(Qt.ItemDataRole.UserRole)) for item in self.filmstrip.selectedItems()}
        if not eligible_only or WORKSPACE.current is None:
            return selected
        return {
            image.id for image in WORKSPACE.current.images
            if image.id in selected and image.status in TRANSLATE_ELIGIBLE_STATUSES
        }

    @staticmethod
    def _translate_selected(image_ids: set[str]) -> None:
        if image_ids:
            WORKSPACE.start_pipeline(image_ids)

    def _translate_selected_from_button(self) -> None:
        self._translate_selected(self._selected_image_ids(eligible_only=True))

    def _retranslate_selected(self, image_ids: set[str]) -> None:
        if not image_ids:
            return
        if confirm(
            self, "Retranslate selected pages",
            "Remove existing OCR, automatic translation, and rendered output for the selected pages, then run OCR, translation, and rendering again? Manual boxes and edits will be kept.",
        ):
            WORKSPACE.start_pipeline(image_ids, retranslate=True)

    def _set_recent_thumbnail(self, image_id: str) -> None:
        try:
            thumbnail = WORKSPACE.set_recent_thumbnail(image_id)
        except ValueError as error:
            QMessageBox.warning(
                self,
                "Could not set thumbnail",
                workspace_action_error(error, action="set recent project thumbnail"),
            )
            return
        self.status.setText(f"Recent project thumbnail set to {thumbnail.name}")

    def _set_identity_recent_thumbnail(self) -> None:
        if self.identity_thumbnail_path is None:
            return
        try:
            thumbnail = WORKSPACE.set_recent_thumbnail_path(self.identity_thumbnail_path)
        except ValueError as error:
            QMessageBox.warning(
                self,
                "Could not set thumbnail",
                workspace_action_error(error, action="set Hydra identity as recent project thumbnail"),
            )
            return
        self.status.setText(f"Recent project thumbnail set to Hydra identity ({thumbnail.name})")

    def _delete_selected_images(self, image_ids: set[str] | None = None) -> None:
        selected_ids = set(image_ids or self._selected_image_ids(eligible_only=False))
        if not selected_ids or WORKSPACE.current is None:
            return
        count = len(selected_ids)
        noun = "image" if count == 1 else "images"
        answer = QMessageBox.question(
            self,
            f"Delete {noun}",
            f"Remove {count} selected {noun} from this project?\n\n"
            "The original image files will remain on disk. Remaining pages will be renumbered automatically.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = WORKSPACE.remove_images(selected_ids)
        except ValueError as error:
            QMessageBox.warning(
                self,
                f"Could not delete {noun}",
                workspace_action_error(error, action=f"delete selected {noun}"),
            )
            return
        if not removed:
            QMessageBox.warning(
                self,
                f"Could not delete {noun}",
                "The selected images could not be removed while translation is running.",
            )
            return
        message = (
            f"{removed} image was removed from the project."
            if removed == 1
            else f"{removed} images were removed from the project."
        )
        self.status.setText(f"{message} Remaining pages were renumbered.")
        QMessageBox.information(
            self,
            "Images deleted",
            f"{message}\n\nThe original files were not deleted.",
        )

