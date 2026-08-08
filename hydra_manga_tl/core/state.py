"""Central UI-facing application state."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class ApplicationState(QObject):
    project_changed = Signal(object)
    selection_changed = Signal(int, int)
    pipeline_changed = Signal(str, int, int, str)
    busy_changed = Signal(bool)
    error_raised = Signal(str)
    dirty_changed = Signal(bool)
    export_changed = Signal(str, int)

    def __init__(self) -> None:
        super().__init__()
        self.project = None
        self.selected_image = -1
        self.selected_block = -1
        self.selected_blocks: set[int] = set()
        self.pipeline_stage = ""
        self.progress_current = 0
        self.progress_total = 0
        self.status_message = "Ready"
        self.busy = False
        self.dirty = False
        self.export_path = ""
        self.exported_count = 0
        self.errors: list[str] = []

    def set_project(self, project) -> None:
        self.project = project
        self.selected_image = 0 if project and project.images else -1
        self.selected_block = -1
        self.selected_blocks.clear()
        self.project_changed.emit(project)
        self.selection_changed.emit(self.selected_image, self.selected_block)

    def refresh_project(self) -> None:
        self.project_changed.emit(self.project)

    def select(self, image_index: int, block_index: int = -1, block_indices: set[int] | list[int] | None = None) -> None:
        self.selected_image = image_index
        self.selected_block = block_index
        if block_indices is not None:
            self.selected_blocks = set(block_indices)
        elif block_index >= 0:
            self.selected_blocks = {block_index}
        else:
            self.selected_blocks.clear()
        self.selection_changed.emit(image_index, block_index)

    def set_pipeline(self, stage: str, current: int, total: int, message: str) -> None:
        self.pipeline_stage = stage
        self.progress_current = current
        self.progress_total = total
        self.status_message = message
        self.pipeline_changed.emit(stage, current, total, message)

    def set_busy(self, busy: bool) -> None:
        if self.busy != busy:
            self.busy = busy
            self.busy_changed.emit(busy)

    def set_dirty(self, dirty: bool) -> None:
        if self.dirty != dirty:
            self.dirty = dirty
            self.dirty_changed.emit(dirty)

    def report_error(self, message: str) -> None:
        self.errors.append(message)
        self.error_raised.emit(message)

    def set_export(self, path: str, count: int) -> None:
        self.export_path = path
        self.exported_count = count
        self.export_changed.emit(path, count)

    def reset(self) -> None:
        self.set_project(None)
        self.set_pipeline("", 0, 0, "Ready")
        self.set_busy(False)
        self.set_dirty(False)
        self.set_export("", 0)
        self.errors.clear()


APP_STATE = ApplicationState()
