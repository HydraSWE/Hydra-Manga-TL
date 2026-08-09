"""GlossaryDialog implementation."""

from __future__ import annotations

from .common import *  # noqa: F401,F403


class GlossaryDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Project Glossary")
        self.setMinimumSize(480, 360)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        
        help_text = QLabel("Enter one protected name or term per line as source = English. These spellings are reused throughout this project.")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        
        self.values = QTextEdit()
        self.values.setPlaceholderText("Example:\n勇者 = Hero\n魔法 = Magic")
        
        glossary = WORKSPACE.current.glossary if WORKSPACE.current else {}
        self.values.setPlainText("\n".join(f"{source} = {target}" for source, target in glossary.items()))
        layout.addWidget(self.values)
        
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self) -> None:
        if WORKSPACE.current is None:
            self.reject()
            return
            
        glossary = {}
        for number, raw in enumerate(self.values.toPlainText().splitlines(), 1):
            if not raw.strip():
                continue
            if "=" not in raw:
                QMessageBox.warning(self, "Invalid glossary", f"Line {number} must use source = English.")
                return
                
            source, target = [value.strip() for value in raw.split("=", 1)]
            if not source or not target:
                QMessageBox.warning(self, "Invalid glossary", f"Line {number} has an empty source or translation.")
                return
                
            glossary[source] = target
            
        WORKSPACE.current.glossary = glossary
        WORKSPACE.save()
        self.accept()


