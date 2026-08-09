"""Manual text-region request lifecycle for the workspace UI."""

from __future__ import annotations

from PySide6.QtCore import QTimer

from hydra_manga_tl.core.settings import SETTINGS
from hydra_manga_tl.core.state import APP_STATE
from hydra_manga_tl.project.manual_region import rect_to_polygon
from hydra_manga_tl.project.workspace import WORKSPACE


class ManualRegionControllerMixin:
    """Own manual drawing and translation callbacks for WorkspaceScreen."""

    def _begin_manual_box(self, mode: str | None = None, *, kind: str = "region") -> None:
        project = WORKSPACE.current
        index = APP_STATE.selected_image
        if project is None or not (0 <= index < len(project.images)):
            return
        mode = "polygon" if mode == "polygon" else "rectangle"
        kind = "title" if kind == "title" else "region"
        self._manual_creation_kind = kind
        self._region_cycle_mode = self._next_region_mode(mode)
        SETTINGS.manual_region_mode = mode
        try:
            SETTINGS.save()
        except OSError:
            pass
        if self.original.begin_manual_selection(mode):
            self._set_region_tool_active(mode, kind)
            message = (
                "Click around the title artwork, double-click to close"
                if kind == "title" and mode == "polygon" else
                "Draw a rectangle around the title artwork"
                if kind == "title" else
                "Click around one translatable text area, double-click to close"
                if mode == "polygon" else
                "Draw a rectangle around one translatable text area"
            )
            self.status.setText(message)
        else:
            self._reset_region_tool()

    def _manual_rect_created(self, rect: list[int]) -> None:
        self._reset_region_tool()
        image_index = APP_STATE.selected_image
        before_state = self._capture_editor_history_state(image_index)
        if WORKSPACE.request_manual_region(image_index, rect):
            if before_state is not None:
                self._pending_manual_history[image_index] = before_state
            return
        self._pending_manual_history.pop(image_index, None)
        self.status.setText("Manual translation is already running")

    def _manual_region_created(self, polygon: list[list[int]]) -> None:
        kind = self._manual_creation_kind
        self._reset_region_tool()
        self._manual_creation_kind = "region"
        image_index = APP_STATE.selected_image
        before_state = self._capture_editor_history_state(image_index)
        if kind == "title":
            if WORKSPACE.request_title_region(image_index, polygon):
                if before_state is not None:
                    self._pending_manual_history[image_index] = before_state
            else:
                self._pending_manual_history.pop(image_index, None)
                self.status.setText("Could not create title reconstruction region")
            return
        if WORKSPACE.request_manual_region(image_index, polygon):
            if before_state is not None:
                self._pending_manual_history[image_index] = before_state
            return
        self._pending_manual_history.pop(image_index, None)
        self.status.setText("Manual translation is already running")

    def _on_manual_region_busy(self, busy: bool) -> None:
        self._manual_busy = busy
        self.add_box.setEnabled(not busy)
        self.title_reconstruction.setEnabled(not busy)
        self.cancel_button.setEnabled(self._job_is_busy or busy)
        self.close_button.setEnabled(not (self._job_is_busy or busy))
        if busy:
            self.status.setText("Processing the selected text box...")
        else:
            self._reset_region_tool()

    def _on_translation_request_state(
        self,
        request_id: str,
        state: str,
        message: str,
    ) -> None:
        if request_id.startswith(("batch:", "selected:")):
            return

        # Save request configuration for potential retries
        request = WORKSPACE.manual_service._active.get(request_id)
        if request:
            self._recent_manual_requests[request_id] = request

        # Retrieve or create overlay
        overlay = self.original._manual_overlays.get(request_id)
        if not overlay:
            title = "Hydra"
            if request_id.startswith("title:") or (request and request.metadata.get("bubble_type") == "title"):
                operation = "Reconstructing Title..."
                stages = ["OCR", "Translation", "Reconstruction"]
            else:
                operation = "Translating Selection..."
                stages = ["OCR", "Translation", "Rendering"]

            polygon = None
            if request:
                polygon = request.metadata.get("polygon")
                if not polygon and request.manual_rect:
                    polygon = rect_to_polygon(request.manual_rect)
            if not polygon:
                polygon = [[0, 0], [100, 0], [100, 100], [0, 100]]

            overlay = self.original.add_manual_overlay(
                request_id, polygon, title=title, operation=operation, stages=stages
            )
            overlay.cancel_requested.connect(lambda rid: WORKSPACE.manual_service.cancel(rid))
            overlay.retry_requested.connect(self._retry_manual_request)

        labels = {
            "queued": "Manual translation queued",
            "ocr": "Reading selected text",
            "translating": "Translating selected text",
            "rendering": "Manual render queued",
            "done": "Manual text box translated",
            "cancelled": "Manual translation cancelled",
            "failed": "Manual translation failed",
        }
        status_msg = message or labels.get(state, state.title())
        self.status.setText(status_msg)

        if state == "queued":
            for stg in overlay.stage_names:
                overlay.update_stage(stg, "waiting")
        elif state == "ocr":
            overlay.update_stage(overlay.stage_names[0], "running")
        elif state == "translating":
            overlay.update_stage(overlay.stage_names[0], "completed")
            overlay.update_stage(overlay.stage_names[1], "running")
        elif state == "rendering":
            overlay.update_stage(overlay.stage_names[0], "completed")
            overlay.update_stage(overlay.stage_names[1], "completed")
            overlay.update_stage(overlay.stage_names[2], "running")
        elif state == "done":
            overlay.show_success("✓ Complete" if request_id.startswith("title:") else "✓ Translation Complete")
            QTimer.singleShot(800, lambda: overlay.start_fade_out())
        elif state == "cancelled":
            overlay.show_cancelled()
            QTimer.singleShot(800, lambda: overlay.start_fade_out())
        elif state == "failed":
            overlay.show_failure(status_msg)

    def _retry_manual_request(self, request_id: str) -> None:
        request = self._recent_manual_requests.get(request_id)
        if request:
            WORKSPACE.manual_service.submit(request)

    def _on_manual_region_finished(self, image_index: int, key: str) -> None:
        before_state = self._pending_manual_history.pop(image_index, None)
        if image_index != APP_STATE.selected_image:
            return
        self._load_image(image_index)
        row = next((index for index, group in enumerate(self._groups) if str(group.get("index")) == key), -1)
        if row >= 0:
            APP_STATE.select(image_index, row)
        group = self._groups[row] if row >= 0 else {}
        label = "Create Title Region" if group.get("bubble_type") == "title" else "Create Manual Region"
        self._push_editor_history(label, image_index, before_state)
        self.status.setText("Title reconstruction region created" if group.get("bubble_type") == "title" else "Manual text box translated")

    def _on_manual_region_failed(self, image_index: int, message: str) -> None:
        self._pending_manual_history.pop(image_index, None)
        if image_index == APP_STATE.selected_image:
            self.status.setText("Manual translation failed")
        # Do not show QMessageBox popup window to satisfy Option C "No popup windows"

    def _cancel_manual_draw(self) -> None:
        self.original.cancel_manual_selection()
        self._reset_region_tool()
        self.status.setText("Manual region drawing cancelled")
        # Also cancel active overlays
        for overlay in list(self.original._manual_overlays.values()):
            overlay._on_cancel_clicked()

