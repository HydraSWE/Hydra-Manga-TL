"""Filmstrip widgets for the workspace."""

from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDrag, QDragEnterEvent, QDragMoveEvent, QDropEvent, QIcon, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import QApplication, QAbstractItemView, QListWidget, QListWidgetItem, QToolTip

from hydra_manga_tl.ui.shared import FILMSTRIP_CARD_SIZE, FILMSTRIP_PREVIEW_SIZE


class ReorderableFilmstrip(QListWidget):
    """Horizontal page list that reports its stable ID order after a move."""

    order_changed = Signal(list)
    reorder_hint = Signal(str)
    add_pages_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._reorder_enabled = True
        self._drop_target_index: int | None = None
        self._drop_bar_x = -1
        self._drop_hover_row = -1
        self._dragged_ids: set[str] = set()
        self._last_moved_ids: set[str] = set()
        self._drag_original_icons: dict[str, QIcon] = {}
        self._drag_original_sizes: dict[str, QSize] = {}
        self._gap_index: int | None = None
        self._plain_press_item: QListWidgetItem | None = None
        self._plain_press_pos = None
        self._autoscroll_direction = 0
        self._autoscroll_timer = QTimer(self)
        self._autoscroll_timer.setInterval(55)
        self._autoscroll_timer.timeout.connect(self._auto_scroll_drag)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)
        self.setDragEnabled(True)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.viewport().setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

    def page_item_count(self) -> int:
        return sum(
            1 for row in range(self.count())
            if self.item(row) is not None and str(self.item(row).data(Qt.ItemDataRole.UserRole) or "") not in {"", "__add_pages__"}
        )

    def create_add_pages_icon(self) -> QIcon:
        size = FILMSTRIP_PREVIEW_SIZE
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = pixmap.rect().adjusted(2, 2, -2, -2)
        painter.setPen(QPen(QColor("#4c566a"), 1.5, Qt.PenStyle.DashLine))
        painter.setBrush(QColor("#1e2330"))
        painter.drawRoundedRect(rect, 6, 6)
        cx = pixmap.width() // 2
        cy = (pixmap.height() // 2) - 4
        pen = QPen(QColor("#69a0ff"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        arm = 11
        painter.drawLine(cx - arm, cy, cx + arm, cy)
        painter.drawLine(cx, cy - arm, cx, cy + arm)
        painter.end()
        return QIcon(pixmap)

    def mousePressEvent(self, event) -> None:
        position = self._event_position(event)
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            item = self.itemAt(position)
            if item is not None:
                if str(item.data(Qt.ItemDataRole.UserRole) or "") == "__add_pages__":
                    item.setSelected(False)
                    self.add_pages_requested.emit()
                else:
                    item.setSelected(not item.isSelected())
                    self.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)
                event.accept()
                return
        is_plain_left = (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        )
        self._plain_press_item = self.itemAt(position) if is_plain_left else None
        self._plain_press_pos = position if self._plain_press_item is not None else None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        position = self._event_position(event)
        press_item = self._plain_press_item
        press_pos = self._plain_press_pos
        self._plain_press_item = None
        self._plain_press_pos = None
        super().mouseReleaseEvent(event)
        if (
            event.button() == Qt.MouseButton.LeftButton
            and press_item is not None
            and press_item is self.itemAt(position)
            and press_pos is not None
            and (position - press_pos).manhattanLength() <= QApplication.startDragDistance()
        ):
            self._select_single_item(press_item)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            item = self.itemAt(self._event_position(event))
            if item is not None:
                self._select_single_item(item)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y() or event.angleDelta().x() or event.pixelDelta().y() or event.pixelDelta().x()
        bar = self.horizontalScrollBar()
        if delta and bar.maximum() > 0:
            bar.setValue(bar.value() - delta)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        modifiers = event.modifiers()
        if modifiers == Qt.KeyboardModifier.ControlModifier and key in {
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
        }:
            self._extend_selection_by_keyboard(-1 if key == Qt.Key.Key_Left else 1)
            event.accept()
            return
        if (
            modifiers == Qt.KeyboardModifier.ControlModifier
            and key == Qt.Key.Key_Space
        ):
            current = self.currentItem()
            if current is not None:
                current.setSelected(not current.isSelected())
            event.accept()
            return
        super().keyPressEvent(event)

    def _extend_selection_by_keyboard(self, direction: int) -> None:
        if self.count() <= 0:
            return
        current_row = self.currentRow()
        if current_row < 0:
            target_row = 0
        else:
            target_row = current_row + (-1 if direction < 0 else 1)
        if not 0 <= target_row < self.count():
            return
        target = self.item(target_row)
        if target is None:
            return
        current = self.currentItem()
        if current is not None:
            current.setSelected(True)
        selection_model = self.selectionModel()
        if selection_model is not None:
            selection_model.setCurrentIndex(
                self.indexFromItem(target),
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )
        target.setSelected(True)
        self.scrollToItem(target, QAbstractItemView.ScrollHint.EnsureVisible)

    def _event_position(self, event):
        return event.position().toPoint() if hasattr(event, "position") else event.pos()

    def _select_single_item(self, item: QListWidgetItem) -> None:
        if str(item.data(Qt.ItemDataRole.UserRole) or "") == "__add_pages__":
            item.setSelected(False)
            self.add_pages_requested.emit()
            return
        self.clearSelection()
        item.setSelected(True)
        self.setCurrentItem(item)

    def set_reorder_enabled(self, enabled: bool) -> None:
        self._reorder_enabled = enabled
        self.setDragEnabled(enabled)
        self.viewport().setAcceptDrops(enabled)
        self.setDropIndicatorShown(enabled)
        self.setToolTip(
            "Drag pages to reorder them. Ctrl/Shift-click selects several pages; "
            "Ctrl+Left/Right grows the keyboard batch."
            if enabled else "Page order cannot be changed while translation is running."
        )

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        self._begin_drag_feedback()
        try:
            indexes = self.selectedIndexes()
            if not indexes:
                return
            drag = QDrag(self)
            drag.setMimeData(self.model().mimeData(indexes))
            current = self.currentItem() or (self.selectedItems()[0] if self.selectedItems() else None)
            if current is not None:
                preview = self._drag_preview_pixmap(current)
                if not preview.isNull():
                    drag.setPixmap(preview)
                    drag.setHotSpot(preview.rect().center())
            drag.exec(supported_actions, Qt.DropAction.MoveAction)
        finally:
            self._clear_drag_feedback()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._reorder_enabled and event.source() is self:
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            return
        event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if not self._reorder_enabled or event.source() is not self:
            event.ignore()
            return
        position = event.position().toPoint()
        self._update_drop_feedback(position)
        self._update_drag_autoscroll(position)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def dragLeaveEvent(self, event) -> None:
        self._clear_drop_feedback()
        self._stop_drag_autoscroll()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if not self._reorder_enabled or event.source() is not self:
            event.ignore()
            return
        self._update_drop_feedback(event.position().toPoint())
        target_index = self._drop_target_index if self._drop_target_index is not None else self.count()
        moved = self._move_selected_to(target_index)
        self._clear_drop_feedback()
        self._stop_drag_autoscroll()
        event.setDropAction(Qt.DropAction.MoveAction)
        if moved:
            event.accept()
        else:
            event.ignore()

    def _move_selected_to(self, target_index: int) -> bool:
        before = self.ordered_ids()
        selected = {str(item.data(Qt.ItemDataRole.UserRole)) for item in self.selectedItems()}
        selected_ids = [image_id for image_id in before if image_id in selected]
        if not selected_ids:
            return False
        current_item = self.currentItem()
        current_id = str(current_item.data(Qt.ItemDataRole.UserRole)) if current_item is not None else ""
        selected_positions = [index for index, image_id in enumerate(before) if image_id in selected]
        remaining = [image_id for image_id in before if image_id not in selected]
        adjusted_target = target_index - sum(index < target_index for index in selected_positions)
        adjusted_target = max(0, min(len(remaining), adjusted_target))
        after = remaining[:adjusted_target] + selected_ids + remaining[adjusted_target:]
        if after == before:
            return False

        items = {str(self.item(row).data(Qt.ItemDataRole.UserRole)): self.item(row) for row in range(self.count())}
        blocked = self.blockSignals(True)
        try:
            while self.count():
                self.takeItem(0)
            for image_id in after:
                self.addItem(items[image_id])
            if "__add_pages__" in items:
                self.addItem(items["__add_pages__"])
            if current_id in items:
                self.setCurrentItem(items[current_id])
            self.clearSelection()
            for image_id in selected_ids:
                items[image_id].setSelected(True)
        finally:
            self.blockSignals(blocked)
        self._last_moved_ids = set(selected_ids)
        self.order_changed.emit(after)
        return True

    def ordered_ids(self) -> list[str]:
        return [
            str(self.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self.count())
            if self.item(row) is not None and str(self.item(row).data(Qt.ItemDataRole.UserRole) or "") not in {"", "__add_pages__"}
        ]

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._drop_target_index is None or self._drop_bar_x < 0:
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = 8
        bottom = max(top + 1, self.viewport().height() - 10)
        painter.setPen(QPen(QColor("#69a0ff"), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(self._drop_bar_x, top, self._drop_bar_x, bottom)
        painter.setPen(QPen(QColor("#69a0ff"), 2))
        if 0 <= self._drop_hover_row < self.count():
            rect = self.visualItemRect(self.item(self._drop_hover_row)).adjusted(2, 2, -2, -2)
            painter.drawRoundedRect(rect, 5, 5)
        painter.end()

    def _begin_drag_feedback(self) -> None:
        self._dragged_ids = {str(item.data(Qt.ItemDataRole.UserRole)) for item in self.selectedItems()}
        self._drag_original_icons.clear()
        self._drag_original_sizes = {
            str(self.item(row).data(Qt.ItemDataRole.UserRole)): self.item(row).sizeHint()
            for row in range(self.count())
            if self.item(row) is not None
        }
        for item in self.selectedItems():
            image_id = str(item.data(Qt.ItemDataRole.UserRole))
            icon = item.icon()
            self._drag_original_icons[image_id] = icon
            pixmap = icon.pixmap(FILMSTRIP_PREVIEW_SIZE)
            if pixmap.isNull():
                continue
            muted = QPixmap(pixmap.size())
            muted.fill(Qt.GlobalColor.transparent)
            painter = QPainter(muted)
            painter.setOpacity(0.45)
            painter.drawPixmap(0, 0, pixmap)
            painter.end()
            item.setIcon(QIcon(muted))
        current = self.currentItem() or (self.selectedItems()[0] if self.selectedItems() else None)
        if current is not None:
            drag_pixmap = self._drag_preview_pixmap(current)
            if not drag_pixmap.isNull():
                self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.viewport().update()

    def _drag_preview_pixmap(self, item: QListWidgetItem) -> QPixmap:
        image_id = str(item.data(Qt.ItemDataRole.UserRole))
        source_icon = self._drag_original_icons.get(image_id, item.icon())
        source = source_icon.pixmap(FILMSTRIP_PREVIEW_SIZE)
        if source.isNull():
            return source
        scale = 1.08
        lifted = source.scaled(
            max(1, int(source.width() * scale)),
            max(1, int(source.height() * scale)),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        canvas = QPixmap(lifted.width() + 10, lifted.height() + 10)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 80))
        painter.drawRoundedRect(7, 7, lifted.width(), lifted.height(), 5, 5)
        painter.drawPixmap(2, 2, lifted)
        painter.end()
        return canvas

    def _clear_drag_feedback(self) -> None:
        self._clear_drop_feedback()
        self._stop_drag_autoscroll()
        if self._drag_original_icons:
            for row in range(self.count()):
                item = self.item(row)
                if item is None:
                    continue
                image_id = str(item.data(Qt.ItemDataRole.UserRole))
                if image_id in self._drag_original_icons:
                    item.setIcon(self._drag_original_icons[image_id])
        self._drag_original_icons.clear()
        self._drag_original_sizes.clear()
        self._dragged_ids.clear()
        self.viewport().update()

    def _target_index_at(self, position) -> int:
        target_item = self.itemAt(position)
        page_count = self.page_item_count()
        if target_item is None or str(target_item.data(Qt.ItemDataRole.UserRole) or "") == "__add_pages__":
            return page_count
        target_index = self.row(target_item)
        if position.x() >= self.visualItemRect(target_item).center().x():
            target_index += 1
        return max(0, min(page_count, target_index))

    def _update_drop_feedback(self, position) -> None:
        target_index = self._target_index_at(position)
        if target_index < self.count():
            target_rect = self.visualItemRect(self.item(target_index))
            bar_x = target_rect.left() - max(3, self.spacing() // 2)
            hover_row = target_index
        elif self.count():
            target_rect = self.visualItemRect(self.item(self.count() - 1))
            bar_x = target_rect.right() + max(3, self.spacing() // 2)
            hover_row = self.count() - 1
        else:
            bar_x = 8
            hover_row = -1
        changed = target_index != self._drop_target_index or bar_x != self._drop_bar_x
        self._drop_target_index = target_index
        self._drop_bar_x = bar_x
        self._drop_hover_row = hover_row
        self._apply_gap(target_index)
        if changed:
            selected_ids = self._dragged_ids or {str(item.data(Qt.ItemDataRole.UserRole)) for item in self.selectedItems()}
            before = self.ordered_ids()
            selected_positions = [index for index, image_id in enumerate(before) if image_id in selected_ids]
            display_position = target_index - sum(index < target_index for index in selected_positions) + 1
            display_position = max(1, min(max(1, self.count() - len(selected_positions) + 1), display_position))
            source_text = ""
            if len(selected_ids) == 1:
                source_id = next(iter(selected_ids))
                if source_id in before:
                    source_text = f"Page {before.index(source_id) + 1} -> "
            message = f"{source_text}Position {display_position}"
            QToolTip.showText(self.viewport().mapToGlobal(position), f"Move to {message}", self.viewport())
            self.reorder_hint.emit(f"Move {len(selected_ids)} page{'s' if len(selected_ids) != 1 else ''} to position {display_position}")
        self.viewport().update()

    def _apply_gap(self, target_index: int) -> None:
        if not self._drag_original_sizes:
            self._drag_original_sizes = {
                str(self.item(row).data(Qt.ItemDataRole.UserRole)): self.item(row).sizeHint()
                for row in range(self.count())
                if self.item(row) is not None
            }
        for row in range(self.count()):
            item = self.item(row)
            if item is None:
                continue
            image_id = str(item.data(Qt.ItemDataRole.UserRole))
            original = self._drag_original_sizes.get(image_id, FILMSTRIP_CARD_SIZE)
            if item.sizeHint() != original:
                item.setSizeHint(original)
        self._gap_index = None
        if not self.count():
            return
        gap_row = target_index if target_index < self.count() else self.count() - 1
        item = self.item(gap_row)
        if item is None:
            return
        image_id = str(item.data(Qt.ItemDataRole.UserRole))
        original = self._drag_original_sizes.get(image_id, item.sizeHint())
        item.setSizeHint(QSize(original.width() + 24, original.height()))
        self._gap_index = gap_row

    def _clear_drop_feedback(self) -> None:
        for row in range(self.count()):
            item = self.item(row)
            if item is None:
                continue
            image_id = str(item.data(Qt.ItemDataRole.UserRole))
            original = self._drag_original_sizes.get(image_id)
            if original is not None:
                item.setSizeHint(original)
        self._drop_target_index = None
        self._drop_bar_x = -1
        self._drop_hover_row = -1
        self._gap_index = None
        QToolTip.hideText()
        self.viewport().update()

    def _update_drag_autoscroll(self, position) -> None:
        margin = 34
        if position.x() < margin:
            self._autoscroll_direction = -1
        elif position.x() > self.viewport().width() - margin:
            self._autoscroll_direction = 1
        else:
            self._autoscroll_direction = 0
        if self._autoscroll_direction and not self._autoscroll_timer.isActive():
            self._autoscroll_timer.start()
        elif not self._autoscroll_direction:
            self._stop_drag_autoscroll()

    def _auto_scroll_drag(self) -> None:
        if not self._autoscroll_direction:
            self._stop_drag_autoscroll()
            return
        bar = self.horizontalScrollBar()
        before = bar.value()
        bar.setValue(before + self._autoscroll_direction * 16)
        if bar.value() == before:
            self._stop_drag_autoscroll()

    def _stop_drag_autoscroll(self) -> None:
        self._autoscroll_direction = 0
        self._autoscroll_timer.stop()
