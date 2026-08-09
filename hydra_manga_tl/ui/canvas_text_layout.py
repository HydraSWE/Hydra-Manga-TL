"""Translated-canvas text layout transform helpers."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsRectItem,
    QGraphicsSimpleTextItem,
)


class CanvasTextLayoutMixin:
    def _clear_text_layout_transform(self) -> None:
        self._hide_rotation_hud()
        if self._layout_frame is not None:
            self._scene.removeItem(self._layout_frame)
            self._layout_frame = None
        for handle in self._layout_handles:
            self._scene.removeItem(handle)
        self._layout_handles.clear()
        self._layout_row = -1
        self._layout_drag = None
        self._hovered_layout_handle = None

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
        self._layout_frame.setCursor(Qt.CursorShape.SizeAllCursor)
        self._layout_frame.setTransformOriginPoint(rect.center())
        self._scene.addItem(self._layout_frame)
        handle_cursors = {
            "top-left": Qt.CursorShape.SizeFDiagCursor,
            "bottom-right": Qt.CursorShape.SizeFDiagCursor,
            "top-right": Qt.CursorShape.SizeBDiagCursor,
            "bottom-left": Qt.CursorShape.SizeBDiagCursor,
            "rotate": Qt.CursorShape.PointingHandCursor,
        }
        for role in ("top-left", "top-right", "bottom-left", "bottom-right", "rotate"):
            handle = QGraphicsEllipseItem(-5, -5, 10, 10)
            handle.setData(0, row)
            handle.setData(1, role)
            handle.setPen(QPen(QColor("#081018"), 1))
            handle.setBrush(QColor("#37d3ff"))
            handle.setZValue(9)
            handle.setCursor(handle_cursors[role])
            self._scene.addItem(handle)
            self._layout_handles.append(handle)
        angle = group.get("text_layout", {}).get("angle") if isinstance(group.get("text_layout"), dict) else None
        if angle is not None:
            try:
                self._layout_frame.setRotation(float(angle))
            except (TypeError, ValueError):
                pass
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
        self._layout_frame.setTransformOriginPoint(rect.center())
        self._position_text_layout_handles()

    def _position_text_layout_handles(self) -> None:
        if self._layout_frame is None:
            return
        rect = self._layout_frame.rect()
        scene_positions = {
            "top-left": self._layout_frame.mapToScene(rect.topLeft()),
            "top-right": self._layout_frame.mapToScene(rect.topRight()),
            "bottom-left": self._layout_frame.mapToScene(rect.bottomLeft()),
            "bottom-right": self._layout_frame.mapToScene(rect.bottomRight()),
            "rotate": self._layout_frame.mapToScene(QPointF(rect.center().x(), rect.top() - 24.0)),
        }
        for handle in self._layout_handles:
            role = str(handle.data(1))
            point = scene_positions.get(role)
            if point is not None:
                handle.setRect(-5, -5, 10, 10)
                handle.setPos(point)

    def _layout_corner_hit_test(self, scene_pos: QPointF) -> tuple[str, str] | None:
        if self._layout_frame is None:
            return None
        for handle in self._layout_handles:
            if str(handle.data(1)) != "rotate":
                continue
            if math.hypot(scene_pos.x() - handle.pos().x(), scene_pos.y() - handle.pos().y()) <= 10.0:
                return ("rotate", "rotate")
        rect = self._layout_frame.rect()
        corners = {
            "top-left": self._layout_frame.mapToScene(rect.topLeft()),
            "top-right": self._layout_frame.mapToScene(rect.topRight()),
            "bottom-left": self._layout_frame.mapToScene(rect.bottomLeft()),
            "bottom-right": self._layout_frame.mapToScene(rect.bottomRight()),
        }
        best_role = None
        best_zone = None
        best_dist = float("inf")
        for role, pt in corners.items():
            dist = math.hypot(scene_pos.x() - pt.x(), scene_pos.y() - pt.y())
            if dist <= 6.0:
                if dist < best_dist:
                    best_dist = dist
                    best_role = role
                    best_zone = "resize"
            elif dist <= 18.0:
                if best_zone != "resize" and dist < best_dist:
                    best_dist = dist
                    best_role = role
                    best_zone = "rotate"
        if best_role and best_zone:
            return (best_role, best_zone)
        return None

    def _update_handle_hover_states(self, hit: tuple[str, str] | None) -> None:
        if hit == self._hovered_layout_handle:
            return
        self._hovered_layout_handle = hit
        cursors = {
            "top-left": Qt.CursorShape.SizeFDiagCursor,
            "bottom-right": Qt.CursorShape.SizeFDiagCursor,
            "top-right": Qt.CursorShape.SizeBDiagCursor,
            "bottom-left": Qt.CursorShape.SizeBDiagCursor,
            "rotate": Qt.CursorShape.PointingHandCursor,
        }
        for handle in self._layout_handles:
            role = str(handle.data(1))
            if hit is not None and hit[0] == role:
                if hit[1] == "rotate":
                    handle.setPen(QPen(QColor("#ffffff"), 2))
                    handle.setBrush(QColor("#00f0ff"))
                    handle.setCursor(Qt.CursorShape.PointingHandCursor)
                else:
                    handle.setPen(QPen(QColor("#ffffff"), 1))
                    handle.setBrush(QColor("#00f0ff"))
                    handle.setCursor(cursors.get(role, Qt.CursorShape.SizeAllCursor))
            else:
                handle.setPen(QPen(QColor("#081018"), 1))
                handle.setBrush(QColor("#37d3ff"))
                handle.setCursor(cursors.get(role, Qt.CursorShape.SizeAllCursor))

    def _update_rotation_hud(self, center: QPointF, mouse_pos: QPointF, angle: float) -> None:
        if self._rotation_pivot_item is None:
            self._rotation_pivot_item = QGraphicsEllipseItem(-6, -6, 12, 12)
            self._rotation_pivot_item.setPen(QPen(QColor("#00f0ff"), 2))
            self._rotation_pivot_item.setBrush(QColor(55, 211, 255, 60))
            self._rotation_pivot_item.setZValue(12)
            self._scene.addItem(self._rotation_pivot_item)
        self._rotation_pivot_item.setPos(center)
        self._rotation_pivot_item.show()

        if self._rotation_guide_line is None:
            self._rotation_guide_line = QGraphicsLineItem()
            self._rotation_guide_line.setPen(QPen(QColor("#37d3ff"), 1, Qt.PenStyle.DashLine))
            self._rotation_guide_line.setZValue(11)
            self._scene.addItem(self._rotation_guide_line)
        self._rotation_guide_line.setLine(center.x(), center.y(), mouse_pos.x(), mouse_pos.y())
        self._rotation_guide_line.show()

        if self._rotation_angle_text is None:
            self._rotation_angle_text = QGraphicsSimpleTextItem()
            self._rotation_angle_text.setBrush(QColor("#ffffff"))
            self._rotation_angle_text.setZValue(13)
            self._rotation_angle_text.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            self._scene.addItem(self._rotation_angle_text)
        self._rotation_angle_text.setText(f"{angle:.1f}°")
        self._rotation_angle_text.setPos(mouse_pos + QPointF(12, 12))
        self._rotation_angle_text.show()

    def _hide_rotation_hud(self) -> None:
        if self._rotation_pivot_item is not None:
            self._scene.removeItem(self._rotation_pivot_item)
            self._rotation_pivot_item = None
        if self._rotation_guide_line is not None:
            self._scene.removeItem(self._rotation_guide_line)
            self._rotation_guide_line = None
        if self._rotation_angle_text is not None:
            self._scene.removeItem(self._rotation_angle_text)
            self._rotation_angle_text = None
