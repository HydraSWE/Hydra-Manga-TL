"""PhraseMemoryManagerDialog implementation."""

from __future__ import annotations

from typing import Callable

from .common import *  # noqa: F401,F403


class PhraseMemoryOperationWorker(QObject):
    """Runs one Phrase Memory operation away from the Qt main thread."""

    completed = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, operation: str, work: Callable[[], object]) -> None:
        super().__init__()
        self.operation = operation
        self.work = work

    @Slot()
    def run(self) -> None:
        try:
            self.completed.emit(self.operation, self.work())
        except Exception as error:
            self.failed.emit(self.operation, str(error))


class PhraseMemoryManagerDialog(QDialog):
    """Phrase Memory (PM v1) Manager dialog for viewing, editing, verifying, deleting, importing, and exporting entries."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Phrase Memory Manager (PM v1)")
        self.resize(960, 640)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        header = QLabel("Phrase Memory (PM v1)")
        header.setObjectName("Heading")
        layout.addWidget(header)

        sub = QLabel("Deterministic sub-phrase memory learned from validated translations. Entries supply terminology hints to translation providers.")
        sub.setWordWrap(True)
        sub.setObjectName("Muted")
        layout.addWidget(sub)

        filter_layout = QHBoxLayout()
        filter_layout.setSpacing(12)
        filter_label = QLabel("Search:")
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter source or target phrase...")
        self.search_input.textChanged.connect(self._filter_changed)
        self.search_input.setFixedHeight(32)
        
        filter_layout.addWidget(filter_label)
        filter_layout.addWidget(self.search_input, 1)
        layout.addLayout(filter_layout)

        pager_layout = QHBoxLayout()
        pager_layout.setSpacing(8)
        self.page_prev_btn = QPushButton("Previous")
        self.page_prev_btn.clicked.connect(self._previous_page)
        self.page_next_btn = QPushButton("Next")
        self.page_next_btn.clicked.connect(self._next_page)
        self.page_label = QLabel()
        self.page_label.setObjectName("Muted")
        pager_layout.addWidget(self.page_prev_btn)
        pager_layout.addWidget(self.page_next_btn)
        pager_layout.addWidget(self.page_label, 1)
        layout.addLayout(pager_layout)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["ID", "Source Phrase", "Target Phrase", "Uses", "Verified", "Origin"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.MultiSelection)
        self.table.itemDoubleClicked.connect(self._edit_selected)
        self.table.itemSelectionChanged.connect(self._capture_table_selection)
        
        # Modernizing the table look
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        
        layout.addWidget(self.table, 1)

        self.status_label = QLabel("Loading Phrase Memory...")
        self.status_label.setObjectName("Muted")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.stats_label = QLabel()
        self.stats_label.setObjectName("Muted")
        layout.addWidget(self.stats_label)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)
        
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.clicked.connect(self._select_all)
        self.deselect_all_btn = QPushButton("Deselect All")
        self.deselect_all_btn.clicked.connect(self._deselect_all)
        self.approve_selected_btn = QPushButton("Approve Selected")
        self.approve_selected_btn.clicked.connect(self._approve_selected)
        self.edit_btn = QPushButton("Edit Entry")
        self.edit_btn.clicked.connect(self._edit_selected)
        self.verify_btn = QPushButton("Toggle Verified")
        self.verify_btn.clicked.connect(self._toggle_verified_selected)
        self.delete_btn = QPushButton("Delete Entry")
        self.delete_btn.clicked.connect(self._delete_selected)
        self.import_btn = QPushButton("Import (.pmdb/.json)")
        self.import_btn.clicked.connect(self._import_file)
        self.export_btn = QPushButton("Export (.pmdb/.json)")
        self.export_btn.clicked.connect(self._export_file)
        self.clear_btn = QPushButton("Clear All")
        self.clear_btn.clicked.connect(self._clear_all)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        
        self._operation_buttons = (
            self.select_all_btn,
            self.deselect_all_btn,
            self.approve_selected_btn,
            self.edit_btn,
            self.verify_btn,
            self.delete_btn,
            self.import_btn,
            self.export_btn,
            self.clear_btn,
        )

        for btn in (*self._operation_buttons, self.close_btn):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumHeight(32)

        btn_layout.addWidget(self.select_all_btn)
        btn_layout.addWidget(self.deselect_all_btn)
        btn_layout.addWidget(self.approve_selected_btn)
        btn_layout.addWidget(self.edit_btn)
        btn_layout.addWidget(self.verify_btn)
        btn_layout.addWidget(self.delete_btn)
        btn_layout.addWidget(self.import_btn)
        btn_layout.addWidget(self.export_btn)
        btn_layout.addWidget(self.clear_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

        self._all_entries = []
        self._stats = None
        self._selected_entry_ids: set[int] = set()
        self._updating_table = False
        self._pm_thread: QThread | None = None
        self._pm_worker: PhraseMemoryOperationWorker | None = None
        self._table_generation = 0
        self._table_populate_chunk_size = 50
        self._table_page_size = 100
        self._table_page_index = 0
        self._filtered_entries_cache = []
        self._pending_table_status = ""
        self._set_busy(True, "Loading Phrase Memory...")
        self.refresh()

    def refresh(self) -> None:
        self._start_pm_operation(
            "load",
            "Loading Phrase Memory...",
            self._load_phrase_memory,
        )

    def _apply_filter(self, done_status: str = "") -> None:
        filtered = self._filtered_entries()
        self._filtered_entries_cache = filtered
        self._selected_entry_ids.intersection_update(
            {int(e.id) for e in self._all_entries if e.id is not None}
        )
        page_size = max(1, int(getattr(self, "_table_page_size", 100) or 100))
        total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
        self._table_page_index = max(0, min(self._table_page_index, total_pages - 1))
        page_start = self._table_page_index * page_size
        page_stop = min(len(filtered), page_start + page_size)
        page_entries = filtered[page_start:page_stop]
        self._table_generation += 1
        generation = self._table_generation
        chunk_size = max(1, int(getattr(self, "_table_populate_chunk_size", 50) or 50))
        self._pending_table_status = done_status
        self._updating_table = True
        self.table.setUpdatesEnabled(False)
        self.table.clearSelection()
        self.table.setRowCount(len(page_entries))

        def populate_row(row: int, entry) -> None:
            entry_id = int(entry.id) if entry.id is not None else None
            id_item = QTableWidgetItem(str(entry_id or ""))
            id_item.setData(Qt.ItemDataRole.UserRole, entry_id)
            src_item = QTableWidgetItem(entry.source_phrase)
            tgt_item = QTableWidgetItem(entry.target_phrase)
            uses_item = QTableWidgetItem(str(entry.usage_count))
            ver_item = QTableWidgetItem("Verified" if entry.verified else "Unverified")
            orig_item = QTableWidgetItem(entry.origin)
            
            for item in (id_item, src_item, tgt_item, uses_item, ver_item, orig_item):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            self.table.setItem(row, 0, id_item)
            self.table.setItem(row, 1, src_item)
            self.table.setItem(row, 2, tgt_item)
            self.table.setItem(row, 3, uses_item)
            self.table.setItem(row, 4, ver_item)
            self.table.setItem(row, 5, orig_item)
            if entry_id in self._selected_entry_ids:
                self.table.selectRow(row)

        def finish_population() -> None:
            if generation != self._table_generation:
                return
            self.table.setUpdatesEnabled(True)
            self._updating_table = False
            self._update_stats_label()
            if self._pending_table_status:
                self.status_label.setText(self._pending_table_status)
            elif filtered:
                self.status_label.setText(
                    f"Showing {page_start + 1:,}-{page_stop:,} of {len(filtered):,} Phrase Memory entries."
                )
            else:
                self.status_label.setText("No Phrase Memory entries match the current search.")
            self.progress_bar.setVisible(False)
            self.search_input.setEnabled(True)
            self.table.setEnabled(True)
            for button in self._operation_buttons:
                button.setEnabled(True)
            self._update_page_controls()

        def populate_chunk(start: int) -> None:
            if generation != self._table_generation:
                return
            stop = min(len(page_entries), start + chunk_size)
            for row in range(start, stop):
                populate_row(row, page_entries[row])
            if stop < len(page_entries):
                self.table.setUpdatesEnabled(True)
                self.status_label.setText(
                    f"Rendering {page_start + stop:,} of {len(filtered):,} Phrase Memory entries..."
                )
                QTimer.singleShot(0, lambda next_start=stop: populate_chunk(next_start))
                return
            finish_population()

        if len(page_entries) > chunk_size:
            self.progress_bar.setVisible(True)
            self.search_input.setEnabled(False)
            self.table.setEnabled(False)
            for button in self._operation_buttons:
                button.setEnabled(False)
            self.page_prev_btn.setEnabled(False)
            self.page_next_btn.setEnabled(False)
            populate_chunk(0)
            return
        for row, entry in enumerate(page_entries):
            populate_row(row, entry)
        finish_population()

    def _filter_changed(self) -> None:
        self._table_page_index = 0
        self._apply_filter()

    def _previous_page(self) -> None:
        if self._table_page_index <= 0:
            return
        self._capture_table_selection()
        self._table_page_index -= 1
        self._apply_filter()

    def _next_page(self) -> None:
        page_size = max(1, int(getattr(self, "_table_page_size", 100) or 100))
        total_pages = max(1, (len(self._filtered_entries_cache) + page_size - 1) // page_size)
        if self._table_page_index >= total_pages - 1:
            return
        self._capture_table_selection()
        self._table_page_index += 1
        self._apply_filter()

    def _update_page_controls(self) -> None:
        total = len(self._filtered_entries_cache)
        page_size = max(1, int(getattr(self, "_table_page_size", 100) or 100))
        total_pages = max(1, (total + page_size - 1) // page_size)
        self.page_prev_btn.setEnabled(self._table_page_index > 0)
        self.page_next_btn.setEnabled(self._table_page_index < total_pages - 1)
        if total:
            page_start = self._table_page_index * page_size + 1
            page_stop = min(total, self._table_page_index * page_size + page_size)
            self.page_label.setText(
                f"Page {self._table_page_index + 1:,} of {total_pages:,} · {page_start:,}-{page_stop:,} of {total:,}"
            )
        else:
            self.page_label.setText("Page 1 of 1 · 0 entries")

    def _filtered_entries(self):
        query = self.search_input.text().strip().casefold()
        return [
            e for e in self._all_entries
            if not query or query in e.source_phrase.casefold() or query in e.target_phrase.casefold()
        ]

    def _capture_table_selection(self) -> None:
        if self._updating_table:
            return
        visible_ids: set[int] = set()
        selected_visible_ids: set[int] = set()
        for row in range(self.table.rowCount()):
            entry_id = self._entry_id_for_row(row)
            if entry_id is None:
                continue
            visible_ids.add(entry_id)
        for index in self.table.selectionModel().selectedRows():
            entry_id = self._entry_id_for_row(index.row())
            if entry_id is not None:
                selected_visible_ids.add(entry_id)
        self._selected_entry_ids.difference_update(visible_ids)
        self._selected_entry_ids.update(selected_visible_ids)
        self._update_stats_label()

    def _entry_id_for_row(self, row: int) -> int | None:
        id_item = self.table.item(row, 0)
        if id_item is None:
            return None
        entry_id = id_item.data(Qt.ItemDataRole.UserRole)
        return int(entry_id) if entry_id is not None else None

    def _selected_entry_id(self) -> int | None:
        if len(self._selected_entry_ids) == 1:
            return next(iter(self._selected_entry_ids))
        row = self.table.currentRow()
        return self._entry_id_for_row(row) if row >= 0 else None

    def _select_all(self) -> None:
        self._selected_entry_ids.update(
            int(e.id) for e in self._filtered_entries() if e.id is not None
        )
        self._apply_filter(f"Selected {len(self._selected_entry_ids):,} Phrase Memory entries.")

    def _deselect_all(self) -> None:
        self._selected_entry_ids.clear()
        self.table.clearSelection()
        self._update_stats_label()
        self.status_label.setText("Selection cleared.")

    def _approve_selected(self) -> None:
        if not self._selected_entry_ids:
            QMessageBox.information(self, "Phrase Memory", "Select entries to approve.")
            return
        selected_ids = sorted(self._selected_entry_ids)
        self._start_pm_operation(
            "approve",
            f"Approving {len(selected_ids):,} selected Phrase Memory entries...",
            lambda: self._approve_phrase_memory_entries(selected_ids),
        )

    def _edit_selected(self) -> None:
        entry_id = self._selected_entry_id()
        if entry_id is None:
            QMessageBox.information(self, "Phrase Memory", "Select one entry to edit.")
            return
        if len(self._selected_entry_ids) > 1:
            QMessageBox.information(self, "Phrase Memory", "Edit one Phrase Memory entry at a time.")
            return
        entry = next((e for e in self._all_entries if e.id == entry_id), None)
        if entry is None:
            return
        new_target, ok = QInputDialog.getText(
            self,
            "Edit Phrase Memory Entry",
            f"Edit translation for '{entry.source_phrase}':",
            QLineEdit.EchoMode.Normal,
            entry.target_phrase,
        )
        if ok and new_target.strip():
            PHRASE_MEMORY.update_entry(entry_id, target_phrase=new_target.strip(), verified=True)
            self.refresh()

    def _toggle_verified_selected(self) -> None:
        entry_id = self._selected_entry_id()
        if entry_id is None:
            QMessageBox.information(self, "Phrase Memory", "Select an entry to toggle verification.")
            return
        if len(self._selected_entry_ids) > 1:
            QMessageBox.information(self, "Phrase Memory", "Use Approve Selected for multiple entries.")
            return
        PHRASE_MEMORY.toggle_verified(entry_id)
        self.refresh()

    def _delete_selected(self) -> None:
        entry_id = self._selected_entry_id()
        if entry_id is None:
            QMessageBox.information(self, "Phrase Memory", "Select an entry to delete.")
            return
        answer = QMessageBox.question(
            self,
            "Delete Entry",
            "Delete this Phrase Memory entry? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            PHRASE_MEMORY.delete_entry(entry_id)
            self._selected_entry_ids.discard(entry_id)
            self.refresh()

    def _import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Phrase Memory",
            "",
            "Phrase Memory (*.pmdb *.json *.db *.sqlite *.sqlite3);;All files (*.*)",
        )
        if not path:
            return
        self._start_pm_operation(
            "import",
            "Importing Phrase Memory entries...",
            lambda: self._import_phrase_memory(Path(path)),
        )

    def _export_file(self) -> None:
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export Phrase Memory",
            "",
            "Hydra PMDB (*.pmdb);;Hydra JSON (*.json);;Hydra SQLite (*.db)",
        )
        if not path:
            return
        suffix = Path(path).suffix.casefold()
        if not suffix:
            suffix = ".json" if "JSON" in selected_filter else ".pmdb"
            path += suffix
        self._start_pm_operation(
            "export",
            "Exporting Phrase Memory...",
            lambda: PHRASE_MEMORY.export(Path(path)),
        )

    def _clear_all(self) -> None:
        answer = QMessageBox.question(
            self,
            "Clear Phrase Memory",
            "Delete every saved Phrase Memory entry and statistics? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._selected_entry_ids.clear()
            self._start_pm_operation(
                "clear",
                "Clearing Phrase Memory...",
                self._clear_phrase_memory,
            )

    def _start_pm_operation(
        self,
        operation: str,
        status: str,
        work: Callable[[], object],
    ) -> None:
        if self._pm_thread is not None and self._pm_thread.isRunning():
            self.status_label.setText("Phrase Memory is busy. Wait for the current operation to finish.")
            return
        self._set_busy(True, status)
        self._pm_thread = QThread(self)
        self._pm_worker = PhraseMemoryOperationWorker(operation, work)
        self._pm_worker.moveToThread(self._pm_thread)
        self._pm_thread.started.connect(self._pm_worker.run)
        self._pm_worker.completed.connect(self._pm_operation_completed)
        self._pm_worker.failed.connect(self._pm_operation_failed)
        self._pm_worker.completed.connect(self._pm_thread.quit)
        self._pm_worker.failed.connect(self._pm_thread.quit)
        self._pm_thread.finished.connect(self._pm_worker.deleteLater)
        self._pm_thread.finished.connect(self._pm_thread.deleteLater)
        self._pm_thread.finished.connect(self._clear_pm_worker)
        self._pm_thread.start()

    @Slot(str, object)
    def _pm_operation_completed(self, operation: str, result: object) -> None:
        if operation == "load":
            entries, stats = result
            self._all_entries = list(entries)
            self._stats = stats
            self._apply_filter(f"Loaded {len(self._all_entries):,} Phrase Memory entries.")
            return
        elif operation == "approve":
            approved, entries, stats = result
            self._all_entries = list(entries)
            self._stats = stats
            self._selected_entry_ids.clear()
            self._apply_filter(f"Approved {approved:,} selected Phrase Memory entries.")
            return
        elif operation == "import":
            imported, entries, stats = result
            self._all_entries = list(entries)
            self._stats = stats
            self._selected_entry_ids.clear()
            self._apply_filter(f"Imported {imported:,} Phrase Memory entries.")
            QMessageBox.information(self, "Phrase Memory Imported", f"Imported {imported:,} phrase memory entries.")
            return
        elif operation == "export":
            QMessageBox.information(self, "Phrase Memory Exported", f"Exported Phrase Memory:\n{result}")
            self.status_label.setText("Phrase Memory export completed.")
        elif operation == "clear":
            entries, stats = result
            self._all_entries = list(entries)
            self._stats = stats
            self._selected_entry_ids.clear()
            self._apply_filter("Phrase Memory cleared.")
            return
        self._set_busy(False)

    @Slot(str, str)
    def _pm_operation_failed(self, operation: str, error: str) -> None:
        action = {
            "import": "import",
            "export": "export",
            "approve": "approve",
            "load": "load",
            "clear": "clear",
        }.get(operation, operation)
        self.status_label.setText(f"Phrase Memory {action} failed.")
        QMessageBox.warning(
            self,
            "Phrase Memory Failed",
            memory_transfer_error(Exception(error), action=action, memory_name="Phrase Memory"),
        )
        self._set_busy(False)

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        if status:
            self.status_label.setText(status)
        self.progress_bar.setVisible(busy)
        self.search_input.setEnabled(not busy)
        self.table.setEnabled(not busy)
        for button in self._operation_buttons:
            button.setEnabled(not busy)
        if hasattr(self, "page_prev_btn"):
            if busy:
                self.page_prev_btn.setEnabled(False)
                self.page_next_btn.setEnabled(False)
            else:
                self._update_page_controls()

    @Slot()
    def _clear_pm_worker(self) -> None:
        self._pm_thread = None
        self._pm_worker = None

    def _update_stats_label(self) -> None:
        if self._stats is None:
            self.stats_label.setText(f"Selected: {len(self._selected_entry_ids):,}")
            return
        self.stats_label.setText(
            f"Total entries: {self._stats.total_entries:,} · "
            f"Verified: {self._stats.verified_entries:,} · "
            f"Pending: {self._stats.pending_entries:,} · "
            f"Selected: {len(self._selected_entry_ids):,} · "
            f"Total matches: {self._stats.total_matches:,} · "
            f"Learned: {self._stats.learned_count:,}"
        )

    def _load_phrase_memory(self):
        return PHRASE_MEMORY.all_entries(), PHRASE_MEMORY.statistics()

    def _approve_phrase_memory_entries(self, entry_ids: list[int]):
        approved = PHRASE_MEMORY.approve_entries(entry_ids)
        entries, stats = self._load_phrase_memory()
        return approved, entries, stats

    def _import_phrase_memory(self, path: Path):
        imported = PHRASE_MEMORY.import_file(path)
        entries, stats = self._load_phrase_memory()
        return imported, entries, stats

    def _clear_phrase_memory(self):
        PHRASE_MEMORY.clear()
        return self._load_phrase_memory()

    def _request_pm_worker_stop(self) -> bool:
        if self._pm_thread is not None and self._pm_thread.isRunning():
            self._pm_thread.requestInterruption()
            self._pm_thread.quit()
            self.status_label.setText("Finishing Phrase Memory operation...")
            return False
        return True

    def accept(self) -> None:
        if not self._request_pm_worker_stop():
            return
        self._table_generation += 1
        super().accept()

    def reject(self) -> None:
        if not self._request_pm_worker_stop():
            return
        self._table_generation += 1
        super().reject()

    def closeEvent(self, event) -> None:
        self._table_generation += 1
        if not self._request_pm_worker_stop():
            event.ignore()
            return
        super().closeEvent(event)


