"""Canvas and manual-region interaction widgets."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF, QPixmap, QWheelEvent
from PySide6.QtWidgets import QLabel, QGraphicsEllipseItem, QGraphicsItem, QGraphicsPixmapItem, QGraphicsPolygonItem, QGraphicsRectItem, QGraphicsScene, QGraphicsView, QPushButton

from hydra_manga_tl.project.manual_region import normalize_image_polygon, normalize_image_rect, polygon_bounding_rect, rect_to_polygon
from hydra_manga_tl.ui.overlay import OverlayProgressWidget


class PolygonVertexHandle(QGraphicsEllipseItem):
    def __init__(self, canvas: "CanvasView", index: int, point: QPointF) -> None:
        super().__init__(-5, -5, 10, 10)
        self.canvas = canvas
        self.index = index
        self.setPos(point)
        self.setPen(QPen(QColor("#ffd35a"), 2))
        self.setBrush(QColor("#2d1b55"))
        self.setZValue(7)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.canvas._polygon_handle_moved()
        return super().itemChange(change, value)


class CanvasView(QGraphicsView):
    region_selected = Signal(int)
    text_layout_changed = Signal(int, object)
    manual_rect_created = Signal(object)
    manual_region_created = Signal(object)
    manual_region_message = Signal(str)
    manual_selection_finished = Signal()
    zoom_changed = Signal(float)

    def __init__(self, title: str) -> None:
        super().__init__()
        self.title = title
        self.setMinimumWidth(300)
        self._scene = QGraphicsScene(self); self.setScene(self._scene)
        self._pixmap = QGraphicsPixmapItem(); self._scene.addItem(self._pixmap)
        self._regions: list[QGraphicsPolygonItem] = []
        self._layout_frame: QGraphicsRectItem | None = None
        self._layout_handles: list[QGraphicsRectItem] = []
        self._layout_row = -1
        self._layout_drag: dict | None = None
        self._manual_mode = ""
        self._manual_start: QPointF | None = None
        self._manual_preview: QGraphicsRectItem | None = None
        self._polygon_points: list[QPointF] = []
        self._polygon_preview: QGraphicsPolygonItem | None = None
        self._polygon_handles: list[PolygonVertexHandle] = []
        self._polygon_confirm = QPushButton("Confirm", self.viewport())
        self._polygon_cancel = QPushButton("Cancel", self.viewport())
        self._polygon_confirm.clicked.connect(self._confirm_polygon_selection)
        self._polygon_cancel.clicked.connect(self.cancel_manual_selection)
        for button in (self._polygon_confirm, self._polygon_cancel):
            button.setObjectName("CanvasOverlayButton")
            button.hide()
        self._zoom = 1.0
        # Manga line art looks soft when Qt applies bilinear filtering while a
        # fitted image is slightly enlarged. Smooth only true downscaling;
        # preserve source pixels at 100% and above.
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.badge = QLabel(title, self.viewport())
        self.badge.setObjectName("CanvasBadge")
        self.badge.move(10, 10)
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.badge.adjustSize()
        self.badge.raise_()
        self._manual_overlays: dict[str, OverlayProgressWidget] = {}
        self.horizontalScrollBar().valueChanged.connect(self.reposition_overlays)
        self.verticalScrollBar().valueChanged.connect(self.reposition_overlays)
        self.zoom_changed.connect(self.reposition_overlays)

    def set_badge(self, text: str) -> None:
        self.badge.setText(text)
        self.badge.adjustSize()
        self.badge.raise_()

    def set_content(self, image_path: Path | None, groups: list[dict], selected: int = -1) -> None:
        self.cancel_manual_selection()
        self.clear_manual_overlays()
        self._clear_text_layout_transform()
        for item in self._regions: self._scene.removeItem(item)
        self._regions.clear()
        pixmap = QPixmap(str(image_path)) if image_path and image_path.is_file() else QPixmap()
        self._pixmap.setPixmap(pixmap); self._scene.setSceneRect(self._pixmap.boundingRect())
        self._update_pixmap_filter()
        for row, group in enumerate(groups):
            polygon = QPolygonF([QPointF(point[0], point[1]) for point in group.get("polygon", [])])
            item = QGraphicsPolygonItem(polygon)
            item.setData(0, row)
            active = row == selected
            manual = bool(group.get("manual"))
            inactive_color = "#b36cff" if manual else "#4d91ff"
            if self.title == "Translated" and active:
                item.setPen(QPen(QColor(255, 211, 90, 120), 1, Qt.PenStyle.DotLine))
                item.setBrush(QColor(255, 211, 90, 8))
            else:
                item.setPen(QPen(QColor("#ffd35a" if active else inactive_color), 3 if active else (2 if manual else 1)))
                item.setBrush(QColor(255, 211, 90, 28) if active else (QColor(179, 108, 255, 24) if manual else QColor(77, 145, 255, 20)))
            item.setZValue(2); self._scene.addItem(item); self._regions.append(item)
        if self.title == "Translated" and 0 <= selected < len(groups):
            self._show_text_layout_transform(selected, groups[selected])
        if not pixmap.isNull() and self._zoom == 1.0: self.fit_image()

    def begin_manual_selection(self, mode: str = "rectangle") -> bool:
        if self._pixmap.pixmap().isNull():
            return False
        self.cancel_manual_selection()
        self._manual_mode = "polygon" if mode == "polygon" else "rectangle"
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setCursor(Qt.CursorShape.CrossCursor)
        return True

    def cancel_manual_selection(self) -> None:
        was_active = bool(self._manual_mode)
        self._manual_mode = ""
        self._manual_start = None
        self._polygon_points.clear()
        if self._manual_preview is not None:
            self._scene.removeItem(self._manual_preview)
            self._manual_preview = None
        if self._polygon_preview is not None:
            self._scene.removeItem(self._polygon_preview)
            self._polygon_preview = None
        for handle in self._polygon_handles:
            self._scene.removeItem(handle)
        self._polygon_handles.clear()
        self._polygon_confirm.hide()
        self._polygon_cancel.hide()
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.unsetCursor()
        if was_active:
            self.manual_selection_finished.emit()

    def mousePressEvent(self, event) -> None:
        if self._manual_mode == "rectangle" and event.button() == Qt.MouseButton.LeftButton:
            self._manual_start = self.mapToScene(event.position().toPoint())
            self._manual_preview = QGraphicsRectItem()
            self._manual_preview.setPen(QPen(QColor("#b36cff"), 2, Qt.PenStyle.DashLine))
            self._manual_preview.setBrush(QColor(179, 108, 255, 28))
            self._manual_preview.setZValue(5)
            self._scene.addItem(self._manual_preview)
            event.accept()
            return
        if self._manual_mode == "polygon" and event.button() == Qt.MouseButton.LeftButton:
            self._polygon_points.append(self._clamp_scene_point(self.mapToScene(event.position().toPoint())))
            self._update_polygon_preview()
            event.accept()
            return
        if self._layout_frame is not None and event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            while item is not None:
                layout_role = item.data(1)
                if layout_role is not None:
                    scene_pos = self.mapToScene(event.position().toPoint())
                    self._layout_drag = {
                        "role": str(layout_role),
                        "start": scene_pos,
                        "rect": QRectF(self._layout_frame.rect()),
                    }
                    self.setDragMode(QGraphicsView.DragMode.NoDrag)
                    event.accept()
                    return
                item = item.parentItem()
        item = self.itemAt(event.position().toPoint())
        while item is not None:
            row = item.data(0)
            if row is not None:
                self.region_selected.emit(int(row)); break
            item = item.parentItem()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._manual_start is not None and self._manual_preview is not None:
            current = self.mapToScene(event.position().toPoint())
            rect = QRectF(self._manual_start, current).normalized().intersected(self._pixmap.boundingRect())
            self._manual_preview.setRect(rect)
            event.accept()
            return
        if self._layout_drag is not None and self._layout_frame is not None:
            current = self.mapToScene(event.position().toPoint())
            start = self._layout_drag["start"]
            base = QRectF(self._layout_drag["rect"])
            dx = current.x() - start.x()
            dy = current.y() - start.y()
            role = self._layout_drag["role"]
            if role == "body":
                rect = base.translated(dx, dy)
            else:
                rect = QRectF(base)
                if "left" in role:
                    rect.setLeft(rect.left() + dx)
                if "right" in role:
                    rect.setRight(rect.right() + dx)
                if "top" in role:
                    rect.setTop(rect.top() + dy)
                if "bottom" in role:
                    rect.setBottom(rect.bottom() + dy)
                rect = rect.normalized()
            self._set_text_layout_rect(self._clamp_layout_rect(rect))
            event.accept()
            return
        if self._manual_mode == "polygon" and self._polygon_points and not self._polygon_handles:
            points = [*self._polygon_points, self._clamp_scene_point(self.mapToScene(event.position().toPoint()))]
            self._set_polygon_preview(points, closed=False)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._manual_start is not None and event.button() == Qt.MouseButton.LeftButton:
            end = self.mapToScene(event.position().toPoint())
            start = (self._manual_start.x(), self._manual_start.y())
            finish = (end.x(), end.y())
            size = (self._pixmap.pixmap().width(), self._pixmap.pixmap().height())
            rect = normalize_image_rect(start, finish, size)
            self.cancel_manual_selection()
            if rect is not None:
                self.manual_rect_created.emit(rect)
                self.manual_region_created.emit(rect_to_polygon(rect))
            else:
                self.manual_region_message.emit("Draw a larger region.")
            event.accept()
            return
        if self._layout_drag is not None and event.button() == Qt.MouseButton.LeftButton:
            self._layout_drag = None
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            if self._layout_frame is not None and self._layout_row >= 0:
                rect = self._layout_frame.rect()
                self.text_layout_changed.emit(self._layout_row, {
                    "x": round(rect.left()),
                    "y": round(rect.top()),
                    "width": max(1, round(rect.width())),
                    "height": max(1, round(rect.height())),
                })
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self._manual_mode == "polygon" and event.button() == Qt.MouseButton.LeftButton:
            if not self._polygon_handles:
                point = self._clamp_scene_point(self.mapToScene(event.position().toPoint()))
                if not self._polygon_points or self._polygon_points[-1] != point:
                    self._polygon_points.append(point)
                self._finish_polygon_draft()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_polygon_buttons()

    def _clamp_scene_point(self, point: QPointF) -> QPointF:
        bounds = self._pixmap.boundingRect()
        return QPointF(
            max(bounds.left(), min(bounds.right(), point.x())),
            max(bounds.top(), min(bounds.bottom(), point.y())),
        )

    def _clear_text_layout_transform(self) -> None:
        if self._layout_frame is not None:
            self._scene.removeItem(self._layout_frame)
            self._layout_frame = None
        for handle in self._layout_handles:
            self._scene.removeItem(handle)
        self._layout_handles.clear()
        self._layout_row = -1
        self._layout_drag = None

    def _show_text_layout_transform(self, row: int, group: dict) -> None:
        rect = self._layout_rect_for_group(group)
        if rect is None:
            return
        self._layout_row = row
        self._layout_frame = QGraphicsRectItem(rect)
        self._layout_frame.setData(0, row)
        self._layout_frame.setData(1, "body")
        self._layout_frame.setPen(QPen(QColor("#37d3ff"), 2, Qt.PenStyle.DashLine))
        self._layout_frame.setBrush(QColor(55, 211, 255, 18))
        self._layout_frame.setZValue(8)
        self._scene.addItem(self._layout_frame)
        for role in ("top-left", "top-right", "bottom-left", "bottom-right"):
            handle = QGraphicsRectItem()
            handle.setData(0, row)
            handle.setData(1, role)
            handle.setPen(QPen(QColor("#081018"), 1))
            handle.setBrush(QColor("#37d3ff"))
            handle.setZValue(9)
            self._scene.addItem(handle)
            self._layout_handles.append(handle)
        self._position_text_layout_handles()

    def _layout_rect_for_group(self, group: dict) -> QRectF | None:
        layout = group.get("text_layout")
        if isinstance(layout, dict):
            try:
                return self._clamp_layout_rect(QRectF(
                    int(layout["x"]), int(layout["y"]),
                    int(layout["width"]), int(layout["height"]),
                ))
            except (KeyError, TypeError, ValueError):
                pass
        rect = group.get("manual_rect")
        if isinstance(rect, list) and len(rect) == 4:
            return self._clamp_layout_rect(QRectF(rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]))
        polygon = group.get("polygon", [])
        if not polygon:
            return None
        xs = [int(point[0]) for point in polygon]
        ys = [int(point[1]) for point in polygon]
        return self._clamp_layout_rect(QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)))

    def _clamp_layout_rect(self, rect: QRectF) -> QRectF:
        bounds = self._pixmap.boundingRect()
        min_width, min_height = 20.0, 12.0
        width = max(min_width, min(rect.width(), bounds.width()))
        height = max(min_height, min(rect.height(), bounds.height()))
        left = max(bounds.left(), min(rect.left(), bounds.right() - width))
        top = max(bounds.top(), min(rect.top(), bounds.bottom() - height))
        return QRectF(left, top, width, height)

    def _set_text_layout_rect(self, rect: QRectF) -> None:
        if self._layout_frame is None:
            return
        self._layout_frame.setRect(rect)
        self._position_text_layout_handles()

    def _position_text_layout_handles(self) -> None:
        if self._layout_frame is None:
            return
        rect = self._layout_frame.rect()
        size = 8.0
        positions = {
            "top-left": rect.topLeft(),
            "top-right": rect.topRight(),
            "bottom-left": rect.bottomLeft(),
            "bottom-right": rect.bottomRight(),
        }
        for handle in self._layout_handles:
            point = positions.get(str(handle.data(1)))
            if point is not None:
                handle.setRect(point.x() - size / 2, point.y() - size / 2, size, size)

    def _polygon_as_lists(self, points: list[QPointF] | None = None) -> list[list[int]]:
        values = points if points is not None else [handle.pos() for handle in self._polygon_handles]
        return [[round(point.x()), round(point.y())] for point in values]

    def _update_polygon_preview(self) -> None:
        self._set_polygon_preview(self._polygon_points, closed=False)

    def _set_polygon_preview(self, points: list[QPointF], *, closed: bool) -> None:
        if self._polygon_preview is None:
            self._polygon_preview = QGraphicsPolygonItem()
            self._polygon_preview.setPen(QPen(QColor("#b36cff"), 2, Qt.PenStyle.DashLine))
            self._polygon_preview.setBrush(QColor(179, 108, 255, 28) if closed else QColor(179, 108, 255, 10))
            self._polygon_preview.setZValue(6)
            self._scene.addItem(self._polygon_preview)
        self._polygon_preview.setPolygon(QPolygonF(points))

    def _finish_polygon_draft(self) -> None:
        size = (self._pixmap.pixmap().width(), self._pixmap.pixmap().height())
        polygon = normalize_image_polygon(self._polygon_as_lists(self._polygon_points), size)
        if polygon is None:
            self.manual_region_message.emit("Polygon must have 3+ points, non-zero area, and no crossing edges.")
            return
        self._polygon_points = [QPointF(point[0], point[1]) for point in polygon]
        self._set_polygon_preview(self._polygon_points, closed=True)
        for handle in self._polygon_handles:
            self._scene.removeItem(handle)
        self._polygon_handles = [
            PolygonVertexHandle(self, index, point)
            for index, point in enumerate(self._polygon_points)
        ]
        for handle in self._polygon_handles:
            self._scene.addItem(handle)
        self._position_polygon_buttons()
        self._polygon_confirm.show()
        self._polygon_cancel.show()
        self.manual_region_message.emit("Adjust polygon vertices, then confirm.")

    def _polygon_handle_moved(self) -> None:
        if self._polygon_handles:
            self._set_polygon_preview([handle.pos() for handle in self._polygon_handles], closed=True)
            self._position_polygon_buttons()

    def _position_polygon_buttons(self) -> None:
        points = [handle.pos() for handle in self._polygon_handles] or self._polygon_points
        if not points:
            return
        polygon = self._polygon_as_lists(points)
        rect = polygon_bounding_rect(polygon)
        if rect is None:
            return
        top_right = self.mapFromScene(QPointF(rect[2], rect[1]))
        self._polygon_confirm.adjustSize()
        self._polygon_cancel.adjustSize()
        x = min(max(8, top_right.x() + 8), max(8, self.viewport().width() - 160))
        y = min(max(8, top_right.y()), max(8, self.viewport().height() - 36))
        self._polygon_confirm.move(x, y)
        self._polygon_cancel.move(x + self._polygon_confirm.width() + 6, y)

    def _confirm_polygon_selection(self) -> None:
        size = (self._pixmap.pixmap().width(), self._pixmap.pixmap().height())
        polygon = normalize_image_polygon(self._polygon_as_lists(), size)
        if polygon is None:
            self.manual_region_message.emit("Polygon must have 3+ points, non-zero area, and no crossing edges.")
            return
        self.cancel_manual_selection()
        self.manual_region_created.emit(polygon)

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self._zoom = max(0.15, min(6.0, self._zoom * factor))
        self.setTransform(self.transform().fromScale(self._zoom, self._zoom))
        self._update_pixmap_filter()
        self.zoom_changed.emit(self._zoom)

    def set_zoom(self, zoom: float) -> None:
        self._zoom = zoom; self.resetTransform(); self.scale(zoom, zoom); self._update_pixmap_filter()

    def fit_image(self) -> None:
        if not self._pixmap.pixmap().isNull():
            self.fitInView(self._pixmap, Qt.AspectRatioMode.KeepAspectRatio)
            self._zoom = self.transform().m11(); self._update_pixmap_filter(); self.zoom_changed.emit(self._zoom)

    def actual_size(self) -> None: self.set_zoom(1.0)

    def _update_pixmap_filter(self) -> None:
        mode = (
            Qt.TransformationMode.SmoothTransformation
            if self.transform().m11() < 0.999
            else Qt.TransformationMode.FastTransformation
        )
        self._pixmap.setTransformationMode(mode)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.reposition_overlays()

    def add_manual_overlay(
        self,
        request_id: str,
        polygon: list[list[int]],
        title: str = "Hydra",
        operation: str = "Translating Selection...",
        stages: list[str] | None = None,
    ) -> OverlayProgressWidget:
        if request_id in self._manual_overlays:
            return self._manual_overlays[request_id]

        is_compact = False
        pts_view = [self.mapFromScene(QPointF(pt[0], pt[1])) for pt in polygon]
        if pts_view:
            xs = [pt.x() for pt in pts_view]
            ys = [pt.y() for pt in pts_view]
            w_reg = max(xs) - min(xs)
            h_reg = max(ys) - min(ys)
            if w_reg < 260 or h_reg < 200:
                is_compact = True

        widget = OverlayProgressWidget(
            request_id=request_id,
            parent=self.viewport(),
            title=title,
            operation=operation,
            stages=stages,
            is_compact=is_compact,
        )
        widget.scene_polygon = [QPointF(pt[0], pt[1]) for pt in polygon]
        self._manual_overlays[request_id] = widget
        widget.finished.connect(lambda rid: self._manual_overlays.pop(rid, None))
        widget.show()
        self.reposition_overlays()
        return widget

    def reposition_overlays(self) -> None:
        for overlay in list(self._manual_overlays.values()):
            if not hasattr(overlay, "scene_polygon") or not overlay.scene_polygon:
                continue
            pts_view = [self.mapFromScene(pt) for pt in overlay.scene_polygon]
            xs = [pt.x() for pt in pts_view]
            ys = [pt.y() for pt in pts_view]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            w_reg = max_x - min_x
            h_reg = max_y - min_y

            # Check average brightness of the cropped area in the source image
            pixmap = self._pixmap.pixmap()
            brightness = 0.5
            if not pixmap.isNull():
                image = pixmap.toImage()
                s_xs = [pt.x() for pt in overlay.scene_polygon]
                s_ys = [pt.y() for pt in overlay.scene_polygon]
                s_min_x = max(0, int(min(s_xs)))
                s_max_x = min(image.width() - 1, int(max(s_xs)))
                s_min_y = max(0, int(min(s_ys)))
                s_max_y = min(image.height() - 1, int(max(s_ys)))
                if s_min_x < s_max_x and s_min_y < s_max_y:
                    total_lum = 0.0
                    count = 0
                    step_x = max(1, (s_max_x - s_min_x) // 10)
                    step_y = max(1, (s_max_y - s_min_y) // 10)
                    for y in range(s_min_y, s_max_y + 1, step_y):
                        for x in range(s_min_x, s_max_x + 1, step_x):
                            color = image.pixelColor(x, y)
                            lum = (0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()) / 255.0
                            total_lum += lum
                            count += 1
                    if count > 0:
                        brightness = total_lum / count

            # Dynamically set theme depending on cropped area background brightness
            overlay.set_theme(light_theme=(brightness > 0.5))

            is_compact = (h_reg < 180 or w_reg < 220)
            overlay.set_compact(is_compact)

            target_w = max(140, min(240, int(w_reg)))
            overlay.setFixedWidth(target_w)

            W = overlay.width()
            H = overlay.height()

            # Center/position strictly inside the bounding box of the cropped selection
            x = min_x + (w_reg - W) / 2
            y = min_y + (h_reg - H) / 2

            if w_reg >= W:
                x = max(min_x, min(max_x - W, x))
            if h_reg >= H:
                y = max(min_y, min(max_y - H, y))

            overlay.move(int(x), int(y))

    def clear_manual_overlays(self) -> None:
        for overlay in list(self._manual_overlays.values()):
            overlay.deleteLater()
        self._manual_overlays.clear()
