"""Translation progress and job-panel behavior for the workspace UI."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt

from hydra_manga_tl.project.workspace import WORKSPACE

from .constants import TRANSLATE_ELIGIBLE_STATUSES


class ProgressControllerMixin:
    """Own progress state transitions while using WorkspaceScreen widgets."""

    def _toggle_job_panel(self) -> None:
        if not self._has_job_details:
            return
        self._set_job_panel_expanded(not self._job_panel_expanded, user=True)

    def _set_job_panel_expanded(self, expanded: bool, user: bool = False) -> None:
        if user:
            self._job_collapse_timer.stop()
            self._job_manually_collapsed = not expanded
        self._job_panel_expanded = bool(expanded and self._has_job_details)
        self.job_body.setVisible(self._job_panel_expanded)
        self.job_toggle.setArrowType(Qt.ArrowType.DownArrow if self._job_panel_expanded else Qt.ArrowType.RightArrow)
        self.job_toggle.setToolTip("Hide translation job details" if self._job_panel_expanded else "Show translation job details")

    def _begin_job_panel(self) -> None:
        self._job_collapse_timer.stop()
        self._has_job_details = True
        self._terminal_job_state = ""
        self._job_manually_collapsed = False
        self.job_toggle.setEnabled(True)
        self._set_job_panel_expanded(True)

    def _schedule_job_panel_collapse(self, terminal_state: str) -> None:
        self._terminal_job_state = terminal_state
        if self._job_panel_expanded:
            self._job_collapse_timer.start()

    def _auto_collapse_job_panel(self) -> None:
        self._set_job_panel_expanded(False)

    def _on_pipeline(self, stage: str, current: int, total: int, message: str) -> None:
        if not stage:
            self._reset_progress_display()
            return

        if stage == "analyzing" or (stage in self._PROGRESS_RANGES and not self._has_job_details):
            self._begin_job_panel()

        self.status.setText(message)
        self._progress_stage = stage
        if total > 0:
            self._job_total = total

        if stage in self._PROGRESS_RANGES:
            floor, self._page_progress_ceiling = self._PROGRESS_RANGES[stage]
            if stage == "analyzing":
                self._job_position = 1 if self._job_total else 0
                self._completed_pages = 0
                self._page_progress_value = 0.0
                self._job_failure_count = 0
            else:
                self._current_job_filename = self._progress_filename(current, message)
                if current != self._job_position:
                    self._job_position = current
                    self._completed_pages = max(0, current - 1)
                    self._page_progress_value = floor
                else:
                    self._page_progress_value = max(self._page_progress_value, floor)
            self._active_page_in_overall = True
            if not self._progress_timer.isActive():
                self._progress_timer.start()
        elif stage == "complete":
            self._progress_timer.stop()
            self._job_position = current
            self._completed_pages = current
            self._active_page_in_overall = False
            self._page_progress_value = 100.0
            self._current_job_filename = self._progress_filename(current, message) or self._current_job_filename
        elif stage == "failed":
            self._progress_timer.stop()
            self._job_position = max(self._job_position, current)
            self._completed_pages = current
            self._active_page_in_overall = False
            self._job_failure_count += 1
            self._current_job_filename = self._progress_filename(current, message) or self._current_job_filename
        elif stage == "cancelled":
            self._progress_timer.stop()
        elif stage == "ready":
            self._progress_timer.stop()
            self._completed_pages = self._job_total
            self._active_page_in_overall = False
            if self._job_failure_count == 0:
                self._page_progress_value = 100.0
                self.status.setText("Translation complete")
            else:
                self.status.setText(f"Translation finished with {self._job_failure_count} failed page(s)")

        self._update_progress_display()
        display_stage = "failed" if stage == "ready" and self._job_failure_count else stage
        self.stage_status.setText(self._stage_text(display_stage))
        if stage == "ready":
            self._schedule_job_panel_collapse("failed" if self._job_failure_count else "complete")
            self._notify_translation_finished()
        elif stage == "cancelled" and not self._job_is_busy:
            self._schedule_job_panel_collapse("cancelled")

    def _notify_translation_finished(self) -> None:
        """Fire desktop notifications at translation pipeline terminal state.

        Called only from the ``stage == 'ready'`` branch of ``_on_pipeline``.
        Reads failure/total counts from the already-updated progress state so
        no extra state needs to be maintained.
        """
        total = self._job_total
        failures = self._job_failure_count
        if total == 0:
            return
        from hydra_manga_tl.core.notifications import NOTIFICATION_SERVICE, NotificationEvent
        from hydra_manga_tl.project.workspace import WORKSPACE
        if failures:
            NOTIFICATION_SERVICE.notify(
                NotificationEvent.TRANSLATION_FAILED,
                "Translation finished with errors",
                f"{total - failures} page(s) done, {failures} failed.",
            )
        else:
            NOTIFICATION_SERVICE.notify(
                NotificationEvent.TRANSLATION_COMPLETED,
                "Translation complete",
                f"{total} page(s) translated successfully.",
            )
        # Review queue is a separate, additive notification
        project = WORKSPACE.current
        if project:
            review_count = sum(1 for img in project.images if img.status == "review")
            if review_count:
                NOTIFICATION_SERVICE.notify(
                    NotificationEvent.REVIEW_QUEUE,
                    "Review queue",
                    f"{review_count} page(s) need review.",
                )

    def _advance_progress_animation(self) -> None:
        if self._progress_stage not in self._PROGRESS_RANGES:
            self._progress_timer.stop()
            return
        remaining = self._page_progress_ceiling - self._page_progress_value
        if remaining <= 0.01:
            self._page_progress_value = self._page_progress_ceiling
            self._update_progress_display()
            return
        self._page_progress_value = min(
            self._page_progress_ceiling,
            self._page_progress_value + max(0.1, remaining * 0.04),
        )
        self._update_progress_display()

    def _update_progress_display(self) -> None:
        page_value = max(0.0, min(100.0, self._page_progress_value))
        active_fraction = page_value / 100.0 if self._active_page_in_overall else 0.0
        overall = 0.0
        if self._job_total:
            overall = min(100.0, (self._completed_pages + active_fraction) / self._job_total * 100.0)
        if self._progress_stage == "ready":
            overall = 100.0

        self.progress.setValue(round(overall * 10)); self.progress.setFormat(f"{overall:.1f}%")
        self.page_progress.setValue(round(page_value * 10)); self.page_progress.setFormat(f"{page_value:.1f}%")
        self.progress.setVisible(True); self.page_progress.setVisible(True)

        stage_name = {
            "preprocessing": "Preparing", "analyzing": "Preparing", "OCR": "Reading", "ocr": "Reading",
            "translating": "Translating", "rendering": "Rebuilding", "reconstructing": "Rebuilding",
            "review": "Reviewing", "complete": "Complete", "failed": "Failed",
            "cancelled": "Cancelled", "ready": "Complete" if self._job_failure_count == 0 else "Finished with errors",
        }.get(self._progress_stage, "Waiting")
        if self._job_total:
            position = max(1, min(self._job_position or 1, self._job_total))
            filename = f" • {self._current_job_filename}" if self._current_job_filename else ""
            self.current_page_label.setText(f"Page {position}/{self._job_total} • {stage_name}{filename}")
            if self._progress_stage == "ready" and self._job_failure_count:
                summary = f"Finished with errors • {self._job_failure_count} failed"
            elif self._progress_stage == "ready":
                summary = "100.0% • Complete"
            elif self._progress_stage == "cancelled":
                summary = f"{overall:.1f}% • Cancelled"
            else:
                subject = self._current_job_filename or f"page {position}/{self._job_total}"
                summary = f"{overall:.1f}% • {stage_name} {subject}"
            self.job_overall.setText(summary)
        else:
            self.current_page_label.setText(stage_name)
            self.job_overall.setText("Idle")

    @staticmethod
    def _progress_filename(position: int, message: str) -> str:
        if WORKSPACE.current is not None and WORKSPACE.active_job_ids and position > 0:
            job_ids = WORKSPACE.active_job_ids
            if position <= len(job_ids):
                image_id = job_ids[position - 1]
                image = next((item for item in WORKSPACE.current.images if item.id == image_id), None)
                if image is not None:
                    return Path(image.source_path).name
        for separator in (" text in ", ": ", " "):
            if separator in message:
                candidate = message.rsplit(separator, 1)[-1].strip()
                if "." in candidate:
                    return Path(candidate).name
        return ""

    def _reset_progress_display(self) -> None:
        self._progress_timer.stop()
        self._job_collapse_timer.stop()
        self._page_progress_value = 0.0; self._page_progress_ceiling = 0.0
        self._job_position = 0; self._job_total = 0; self._completed_pages = 0
        self._active_page_in_overall = False; self._progress_stage = ""; self._job_failure_count = 0
        self._current_job_filename = ""
        self.progress.setValue(0); self.progress.setFormat("0.0%"); self.progress.hide()
        self.page_progress.setValue(0); self.page_progress.setFormat("0.0%"); self.page_progress.hide()
        self._has_job_details = False; self._terminal_job_state = ""; self._job_manually_collapsed = False
        self.job_toggle.setEnabled(False); self._set_job_panel_expanded(False)
        self.current_page_label.setText("Current page"); self.job_overall.setText("Idle")
        self.status.setText("Ready"); self.stage_status.setText(self._stage_text(""))

    @staticmethod
    def _stage_text(active: str) -> str:
        aliases = {"analyzing": "preprocessing", "OCR": "ocr", "rendering": "reconstructing", "ready": "complete", "done": "complete"}
        active = aliases.get(active, active)
        stages = [("preprocessing", "Preparing"), ("ocr", "Reading"), ("translating", "Translating"), ("reconstructing", "Rendering"), ("review", "Review"), ("complete", "Complete")]
        active_index = next((index for index, (key, _) in enumerate(stages) if key == active), -1)
        if active in {"complete"}:
            active_index = len(stages)
        values = []
        for index, (_, label) in enumerate(stages):
            marker = "[x]" if index < active_index or active == "complete" else ("[>]" if index == active_index else "[ ]")
            values.append(f"{marker} {label}")
        suffix = "     [!] Failed" if active == "failed" else ("     [!] Cancelled" if active == "cancelled" else "")
        return "     ".join(values) + suffix

    def _on_busy(self, busy: bool) -> None:
        self._job_is_busy = busy
        self.filmstrip.set_reorder_enabled(not busy)
        pending = bool(WORKSPACE.current and any(image.status in TRANSLATE_ELIGIBLE_STATUSES for image in WORKSPACE.current.images))
        self.start_button.setEnabled(not busy and pending)
        self.cancel_button.setEnabled(busy or self._manual_busy)
        self.close_button.setEnabled(not (busy or self._manual_busy))
        self.add_box.setEnabled(not self._manual_busy)
        self.title_reconstruction.setEnabled(not self._manual_busy)
        self._selection_changed()
        keep_result = self._job_total > 0 and self._progress_stage in {"ready", "cancelled", "failed", "complete"}
        self.progress.setVisible(busy or keep_result); self.page_progress.setVisible(busy or keep_result)
        if not busy and not keep_result:
            self.progress.setValue(0); self.page_progress.setValue(0)

