"""PhraseMemoryManagerDialog implementation."""

from __future__ import annotations

from .common import *  # noqa: F401,F403


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
        self.search_input.textChanged.connect(self._apply_filter)
        self.search_input.setFixedHeight(32)
        
        filter_layout.addWidget(filter_label)
        filter_layout.addWidget(self.search_input, 1)
        layout.addLayout(filter_layout)

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
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.itemDoubleClicked.connect(self._edit_selected)
        
        # Modernizing the table look
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        
        layout.addWidget(self.table, 1)

        self.stats_label = QLabel()
        self.stats_label.setObjectName("Muted")
        layout.addWidget(self.stats_label)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)
        
        edit_btn = QPushButton("Edit Entry")
        edit_btn.clicked.connect(self._edit_selected)
        verify_btn = QPushButton("Toggle Verified")
        verify_btn.clicked.connect(self._toggle_verified_selected)
        delete_btn = QPushButton("Delete Entry")
        delete_btn.clicked.connect(self._delete_selected)
        import_btn = QPushButton("Import (.pmdb/.json)")
        import_btn.clicked.connect(self._import_file)
        export_btn = QPushButton("Export (.pmdb/.json)")
        export_btn.clicked.connect(self._export_file)
        clear_btn = QPushButton("Clear All")
        clear_btn.clicked.connect(self._clear_all)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        
        for btn in (edit_btn, verify_btn, delete_btn, import_btn, export_btn, clear_btn, close_btn):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumHeight(32)

        btn_layout.addWidget(edit_btn)
        btn_layout.addWidget(verify_btn)
        btn_layout.addWidget(delete_btn)
        btn_layout.addWidget(import_btn)
        btn_layout.addWidget(export_btn)
        btn_layout.addWidget(clear_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        self._all_entries = []
        self.refresh()

    def refresh(self) -> None:
        self._all_entries = PHRASE_MEMORY.all_entries()
        self._apply_filter()
        stats = PHRASE_MEMORY.statistics()
        self.stats_label.setText(
            f"Total entries: {stats.total_entries:,} · Verified: {stats.verified_entries:,} · Total matches: {stats.total_matches:,} · Learned: {stats.learned_count:,}"
        )

    def _apply_filter(self) -> None:
        query = self.search_input.text().strip().casefold()
        filtered = [
            e for e in self._all_entries
            if not query or query in e.source_phrase.casefold() or query in e.target_phrase.casefold()
        ]
        self.table.setRowCount(len(filtered))
        for row, entry in enumerate(filtered):
            id_item = QTableWidgetItem(str(entry.id))
            id_item.setData(Qt.ItemDataRole.UserRole, entry.id)
            src_item = QTableWidgetItem(entry.source_phrase)
            tgt_item = QTableWidgetItem(entry.target_phrase)
            uses_item = QTableWidgetItem(str(entry.usage_count))
            ver_item = QTableWidgetItem("✓ Verified" if entry.verified else "Unverified")
            orig_item = QTableWidgetItem(entry.origin)
            
            self.table.setItem(row, 0, id_item)
            self.table.setItem(row, 1, src_item)
            self.table.setItem(row, 2, tgt_item)
            self.table.setItem(row, 3, uses_item)
            self.table.setItem(row, 4, ver_item)
            self.table.setItem(row, 5, orig_item)

    def _selected_entry_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        id_item = self.table.item(row, 0)
        return id_item.data(Qt.ItemDataRole.UserRole) if id_item else None

    def _edit_selected(self) -> None:
        entry_id = self._selected_entry_id()
        if entry_id is None:
            QMessageBox.information(self, "Phrase Memory", "Select an entry to edit.")
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
        try:
            imported = PHRASE_MEMORY.import_file(Path(path))
            QMessageBox.information(self, "Phrase Memory Imported", f"Imported {imported:,} phrase memory entries.")
            self.refresh()
        except Exception as error:
            QMessageBox.warning(
                self,
                "Import Failed",
                memory_transfer_error(error, action="import", memory_name="Phrase Memory"),
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
        try:
            destination = PHRASE_MEMORY.export(Path(path))
            QMessageBox.information(self, "Phrase Memory Exported", f"Exported Phrase Memory:\n{destination}")
        except Exception as error:
            QMessageBox.warning(
                self,
                "Export Failed",
                memory_transfer_error(error, action="export", memory_name="Phrase Memory"),
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
            PHRASE_MEMORY.clear()
            self.refresh()


