"""Text-layout history helpers for the workspace editor."""

from __future__ import annotations

import json

from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox, QTextEdit

from hydra_manga_tl.core.state import APP_STATE
from hydra_manga_tl.core.user_errors import workspace_action_error
from hydra_manga_tl.project.workspace import WORKSPACE


class EditorHistoryMixin:
    @staticmethod
    def _layout_from_group(group: dict) -> dict | None:
        layout = group.get("text_layout")
        if isinstance(layout, dict):
            try:
                result = {
                    "x": int(layout["x"]),
                    "y": int(layout["y"]),
                    "width": int(layout["width"]),
                    "height": int(layout["height"]),
                }
                if layout.get("angle") is not None:
                    try:
                        result["angle"] = float(layout["angle"])
                    except (TypeError, ValueError):
                        pass
                return result
            except (KeyError, TypeError, ValueError):
                pass
        rect = group.get("manual_rect")
        if isinstance(rect, list) and len(rect) == 4:
            return {
                "x": int(rect[0]), "y": int(rect[1]),
                "width": int(rect[2]) - int(rect[0]),
                "height": int(rect[3]) - int(rect[1]),
            }
        polygon = group.get("polygon", [])
        if not polygon:
            return None
        xs = [int(point[0]) for point in polygon]
        ys = [int(point[1]) for point in polygon]
        return {
            "x": min(xs), "y": min(ys),
            "width": max(xs) - min(xs),
            "height": max(ys) - min(ys),
        }

    @staticmethod
    def _layout_key(image_index: int, group_index) -> tuple[int, str]:
        return (int(image_index), str(group_index))

    def _capture_editor_history_state(self, image_index: int) -> dict | None:
        if WORKSPACE.current is None or not (0 <= image_index < len(WORKSPACE.current.images)):
            return None
        try:
            return WORKSPACE.capture_editor_state(image_index)
        except (OSError, ValueError, TypeError):
            return None

    def _push_editor_history(
        self,
        label: str,
        image_index: int,
        before: dict | None,
        after: dict | None = None,
    ) -> None:
        if before is None:
            return
        after = after if after is not None else self._capture_editor_history_state(image_index)
        if after is None or before == after:
            return
        self._layout_undo.append({
            "kind": "editor_state",
            "label": label,
            "image_index": image_index,
            "before": before,
            "after": after,
        })
        del self._layout_undo[:-200]
        self._layout_redo.clear()

    def _clear_pending_layouts_for_image(self, image_index: int) -> None:
        for key in [
            key for key in self._pending_text_layouts
            if key[0] == int(image_index)
        ]:
            self._pending_text_layouts.pop(key, None)

    def _discard_text_layout_history(self, image_index: int, group_indices: set[str]) -> None:
        def keep(command: dict) -> bool:
            return not (
                command.get("kind") == "text_layout"
                and int(command.get("image_index", -1)) == int(image_index)
                and str(command.get("group_index")) in group_indices
            )

        self._layout_undo = [command for command in self._layout_undo if keep(command)]
        self._layout_redo = [command for command in self._layout_redo if keep(command)]

    def _restore_editor_history_command(self, command: dict, state_key: str) -> bool:
        state = command.get(state_key)
        if not isinstance(state, dict):
            return False
        image_index = int(command.get("image_index", APP_STATE.selected_image))
        working = self._show_working(
            "Undo" if state_key == "before" else "Redo",
            "Restoring editor state...",
        )
        try:
            WORKSPACE.restore_editor_state(
                image_index,
                state,
                log_callback=working.append_log,
            )
            self._clear_pending_layouts_for_image(image_index)
            row = APP_STATE.selected_block if image_index == APP_STATE.selected_image else -1
            self._load_image(image_index, row)
            self.status.setText(
                f"{'Undid' if state_key == 'before' else 'Redid'} {command.get('label', 'editor change')}"
            )
            return True
        except (MemoryError, OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
            QMessageBox.warning(
                self,
                "Could not restore editor state",
                workspace_action_error(error, action="restore the editor state"),
            )
            return False
        finally:
            self._close_working(working)

    def _text_layout_changed(self, row: int, layout: dict) -> None:
        if not (0 <= row < len(self._groups)):
            return
        group = self._groups[row]
        key = self._layout_key(APP_STATE.selected_image, group["index"])
        before = self._pending_text_layouts.get(key) or self._layout_from_group(group)
        after = {
            "x": int(layout["x"]),
            "y": int(layout["y"]),
            "width": int(layout["width"]),
            "height": int(layout["height"]),
        }
        if layout.get("angle") is not None:
            try:
                after["angle"] = float(layout["angle"])
            except (TypeError, ValueError):
                pass
        if before == after:
            return
        command = {
            "kind": "text_layout",
            "image_index": APP_STATE.selected_image,
            "group_index": group["index"],
            "row": row,
            "before": before,
            "after": after,
        }
        if self._apply_text_layout_command(command, after):
            self._layout_undo.append(command)
            del self._layout_undo[:-200]
            self._layout_redo.clear()

    def _apply_text_layout_command(self, command: dict, layout: dict | None) -> bool:
        if layout is None:
            return False
        image_index = int(command["image_index"])
        group_index = command["group_index"]
        row = int(command.get("row", APP_STATE.selected_block))
        staged = {
            "x": int(layout["x"]),
            "y": int(layout["y"]),
            "width": int(layout["width"]),
            "height": int(layout["height"]),
        }
        if layout.get("angle") is not None:
            try:
                staged["angle"] = float(layout["angle"])
            except (TypeError, ValueError):
                pass
        self._pending_text_layouts[self._layout_key(image_index, group_index)] = staged
        if image_index == APP_STATE.selected_image and 0 <= row < len(self._groups):
            self._groups[row]["text_layout"] = dict(staged)
            if row == APP_STATE.selected_block:
                self.translated._set_text_layout_rect(QRectF(staged["x"], staged["y"], staged["width"], staged["height"]))
        self.status.setText("Text layout staged; click Apply & Rerender")
        return True

    @staticmethod
    def _focused_text_entry():
        focus = QApplication.focusWidget()
        return focus if isinstance(focus, (QTextEdit, QLineEdit)) else None

    def _undo_text_layout(self) -> None:
        text_entry = self._focused_text_entry()
        if text_entry is not None:
            text_entry.undo()
            return
        if not self._layout_undo:
            return
        command = self._layout_undo.pop()
        if command.get("kind") == "editor_state":
            if self._restore_editor_history_command(command, "before"):
                self._layout_redo.append(command)
            return
        if self._apply_text_layout_command(command, command.get("before")):
            self._layout_redo.append(command)

    def _redo_text_layout(self) -> None:
        text_entry = self._focused_text_entry()
        if text_entry is not None:
            text_entry.redo()
            return
        if not self._layout_redo:
            return
        command = self._layout_redo.pop()
        if command.get("kind") == "editor_state":
            if self._restore_editor_history_command(command, "after"):
                self._layout_undo.append(command)
            return
        if self._apply_text_layout_command(command, command.get("after")):
            self._layout_undo.append(command)
