"""Resumable chapter job manifest helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any


QUEUE_STATES = {"pending", "preprocessing", "OCR", "translating", "rendering", "review", "done", "failed"}
STALE_ACTIVE_STATES = {"preprocessing", "OCR", "ocr", "translating", "localizing", "rendering", "reconstructing", "review", "analyzing"}


@dataclass
class PageJobState:
    image_id: str
    source_path: str
    state: str = "pending"
    completed_stages: list[str] = field(default_factory=list)
    error: str = ""
    updated_at: str = ""


class JobManifest:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.pages: dict[str, PageJobState] = {}
        self._lock = threading.RLock()

    @classmethod
    def load(cls, path: Path) -> "JobManifest":
        manifest = cls(path)
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            manifest.pages = {
                key: PageJobState(**value)
                for key, value in dict(payload.get("pages", {})).items()
            }
        return manifest

    def ensure_page(self, image_id: str, source_path: str) -> PageJobState:
        page = self.pages.get(image_id)
        if page is None:
            page = PageJobState(image_id=image_id, source_path=source_path)
            self.pages[image_id] = page
        return page

    def mark(self, image_id: str, state: str, *, stage: str | None = None, error: str = "") -> None:
        page = self.pages[image_id]
        page.state = state
        page.error = error
        page.updated_at = datetime.now(timezone.utc).isoformat()
        if stage and stage not in page.completed_stages:
            page.completed_stages.append(stage)
        self.save()

    def recover_stale(self, checkpoint_exists=None) -> dict[str, str]:
        """Normalize interrupted work while preserving completed checkpoints."""
        recovered: dict[str, str] = {}
        for image_id, page in self.pages.items():
            if page.state not in STALE_ACTIVE_STATES:
                continue
            has_ocr = bool(checkpoint_exists(image_id, "OCR")) if checkpoint_exists else "OCR" in page.completed_stages
            page.state = "partial" if has_ocr else "queued"
            page.error = ""
            page.updated_at = datetime.now(timezone.utc).isoformat()
            recovered[image_id] = page.state
        if recovered:
            self.save()
        return recovered

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, Any] = {
                "version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "pages": {key: asdict(value) for key, value in self.pages.items()},
            }
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
