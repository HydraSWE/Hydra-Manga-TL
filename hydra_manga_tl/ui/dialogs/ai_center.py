"""AiCenterDialog implementation."""

from __future__ import annotations

from .common import *  # noqa: F401,F403


class AiCenterDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Hydra AI Dataset Dashboard")
        self.resize(800, 760)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        
        title = QLabel("Hydra AI Center")
        title.setObjectName("Heading")
        layout.addWidget(title)
        
        self.profile_label = QLabel()
        self.profile_label.setObjectName("Muted")
        layout.addWidget(self.profile_label)
        
        intro = QLabel("Dataset readiness • Japanese → English • only explicitly approved corrections count toward training")
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        layout.addWidget(intro)
        
        self.training_state = QLabel()
        self.training_state.setWordWrap(True)
        layout.addWidget(self.training_state)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        
        host = QWidget()
        self.cards_layout = QVBoxLayout(host)
        self.cards_layout.setContentsMargins(0, 8, 8, 8)
        self.cards_layout.setSpacing(12)
        
        self.cards = {}
        tasks = (
            ("OCR Expert", "ocr"), 
            ("Translation Expert", "translation"), 
            ("Bubble Detector", "bubble"),
            ("Layout Expert", "layout"), 
            ("Image Cleaner", "cleaner"), 
            ("Quality Judge", "quality")
        )
        
        for label, task in tasks:
            card = QFrame()
            card.setObjectName("ProgressPanel")
            
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(20, 16, 20, 16)
            card_layout.setSpacing(8)
            
            heading_row = QHBoxLayout()
            heading = QLabel(label)
            heading.setObjectName("JobTitle")
            
            count = QLabel("0 / 0")
            count.setObjectName("Muted")
            
            heading_row.addWidget(heading)
            heading_row.addStretch()
            heading_row.addWidget(count)
            
            progress = QProgressBar()
            progress.setRange(0, 1)
            progress.setValue(0)
            progress.setTextVisible(True)
            progress.setFixedHeight(18)
            
            detail = QLabel("Waiting for approved corrections")
            detail.setObjectName("Muted")
            detail.setWordWrap(True)
            
            button_row = QHBoxLayout()
            button_row.setContentsMargins(0, 8, 0, 0)
            
            dry_run = QPushButton("Dry Run")
            dry_run.setCursor(Qt.CursorShape.PointingHandCursor)
            dry_run.clicked.connect(lambda _=False, value=task: self._dry_run(value))
            
            button = QPushButton("Not Ready")
            button.setEnabled(False)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, value=task: self._queue(value))
            
            button_row.addWidget(dry_run)
            button_row.addStretch()
            button_row.addWidget(button)
            
            card_layout.addLayout(heading_row)
            card_layout.addWidget(progress)
            card_layout.addWidget(detail)
            card_layout.addLayout(button_row)
            
            self.cards_layout.addWidget(card)
            self.cards[task] = {
                "count": count, 
                "progress": progress, 
                "detail": detail, 
                "button": button, 
                "dry_run": dry_run
            }
            
        self.cards_layout.addStretch()
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)
        
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 8, 0, 0)
        
        refresh = QPushButton("Refresh")
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.clicked.connect(self.refresh)
        
        pause = QPushButton("Pause Training")
        pause.setCursor(Qt.CursorShape.PointingHandCursor)
        pause.clicked.connect(self._pause)
        
        resume = QPushButton("Resume Training")
        resume.setCursor(Qt.CursorShape.PointingHandCursor)
        resume.clicked.connect(self._resume)
        
        close = QPushButton("Close")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.accept)
        
        controls.addWidget(refresh)
        controls.addWidget(pause)
        controls.addWidget(resume)
        controls.addStretch()
        controls.addWidget(close)
        
        layout.addLayout(controls)
        self.refresh()

    def refresh(self) -> None:
        payload = HYDRA_AI.model_status()
        style = WORKSPACE.current.text_style if WORKSPACE.current else "Manga"
        self.profile_label.setText(f"Style profile: {style} • Data root: {SETTINGS.ai_data_root}")
        paused = bool(payload.get("paused"))
        self.training_state.setText("Training is paused." if paused else "Training is available only when a model reaches both dataset and golden-set thresholds.")
        progress_data = payload.get("progress", {})
        
        for task, widgets in self.cards.items():
            profile = style if task in {"translation", "layout"} else "global"
            item = progress_data.get(f"{task}:{profile}", {})
            approved = int(item.get("approved", 0))
            required = max(1, int(item.get("required", 1)))
            unit = item.get("unit", "samples")
            
            widgets["count"].setText(f"{approved:,} / {required:,} {unit}")
            widgets["progress"].setRange(0, required)
            widgets["progress"].setValue(min(approved, required))
            widgets["progress"].setFormat(f"%p%  •  %v / {required:,}")
            
            golden = int(item.get("golden", 0))
            required_golden = int(item.get("required_golden", 0))
            details = [f"Golden set: {golden:,} / {required_golden:,}"]
            
            if task == "bubble":
                details.append(f"Approved regions: {int(item.get('regions', 0)):,} / {int(item.get('required_regions', 2000)):,}")
            reasons = item.get("reasons", [])
            
            if reasons:
                details.append("Blocked: " + "; ".join(reasons[:2]))
            elif item.get("ready"):
                details.append("Ready to train")
                
            widgets["detail"].setText(" • ".join(details))
            ready = bool(item.get("ready")) and not paused
            widgets["button"].setEnabled(ready)
            widgets["button"].setText("Queue Training" if ready else "Not Ready")

    def _queue(self, task: str) -> None:
        style = WORKSPACE.current.text_style if WORKSPACE.current else "Manga"
        run_id = HYDRA_AI.queue_training(task, style)
        self.refresh()
        QMessageBox.information(self, "Training queue", f"Run recorded: {run_id}" if run_id else "HydraMangaAi is unavailable.")

    def _dry_run(self, task: str) -> None:
        style = WORKSPACE.current.text_style if WORKSPACE.current else "Manga"
        result = HYDRA_AI.training_dry_run(task, style)
        readiness = result.get("readiness", {})
        reasons = readiness.get("reasons") or result.get("reasons") or ()
        
        lines = [
            f"Task: {result.get('task', task)}",
            f"Profile: {result.get('profile', style)}",
            f"Ready: {'yes' if result.get('ready') else 'no'}",
        ]
        if readiness:
            lines.extend([
                f"Approved pairs: {readiness.get('approved_pairs', 0):,}",
                f"Golden pairs: {readiness.get('golden_pairs', 0):,}",
                f"Projects: {readiness.get('project_count', 0):,}",
            ])
        if reasons:
            lines.append("Blocked: " + "; ".join(str(reason) for reason in reasons))
        if result.get("next_step"):
            lines.append(str(result["next_step"]))
            
        QMessageBox.information(self, "Training dry run", "\n".join(lines))

    def _pause(self) -> None:
        HYDRA_AI.pause_training()
        self.refresh()

    def _resume(self) -> None:
        HYDRA_AI.resume_training()
        self.refresh()


