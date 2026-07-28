"""Typed contracts shared by translation, OCR, and render orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4


class TranslationRequestType(str, Enum):
    BATCH = "batch"
    SELECTED = "selected"
    MANUAL = "manual"
    REVIEW = "review"


class TranslationRequestStatus(str, Enum):
    QUEUED = "queued"
    OCR = "ocr"
    TRANSLATING = "translating"
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TranslationRequest:
    type: TranslationRequestType
    project_id: str
    image_id: str
    image_index: int
    source_path: Path
    target_language: str
    source_language: str = "auto"
    manual_rect: tuple[int, int, int, int] | None = None
    selected_group_ids: tuple[str, ...] = ()
    force_retranslate: bool = False
    render_policy: str = "complete"
    request_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_path", Path(self.source_path))
        if self.manual_rect is not None:
            rect = tuple(int(value) for value in self.manual_rect)
            if len(rect) != 4 or rect[2] <= rect[0] or rect[3] <= rect[1]:
                raise ValueError("manual_rect must be a non-empty x1, y1, x2, y2 rectangle")
            object.__setattr__(self, "manual_rect", rect)
        if self.type is TranslationRequestType.MANUAL and self.manual_rect is None:
            raise ValueError("Manual translation requests require manual_rect")
        if not self.project_id or not self.image_id:
            raise ValueError("Translation requests require project_id and image_id")

    @classmethod
    def from_legacy_manual(cls, payload: dict[str, Any]) -> "TranslationRequest":
        return cls(
            request_id=str(payload.get("request_id") or uuid4()),
            type=TranslationRequestType.MANUAL,
            project_id=str(payload["project_id"]),
            image_id=str(payload["image_id"]),
            image_index=int(payload["image_index"]),
            source_path=Path(payload["source_path"]),
            target_language=str(payload["target"]),
            source_language=str(payload.get("source_language") or "auto"),
            manual_rect=tuple(int(value) for value in payload["rect"]),
            render_policy=str(payload.get("render_policy") or "complete"),
            metadata=dict(payload),
        )


@dataclass(frozen=True)
class RenderRequest:
    request_id: str
    project_id: str
    image_id: str
    image_index: int
    result_path: Path
    render_dir: Path
    source_path: Path
    reason: str
    render_policy: str = "complete"

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_path", Path(self.result_path))
        object.__setattr__(self, "render_dir", Path(self.render_dir))
        object.__setattr__(self, "source_path", Path(self.source_path))
        if self.reason not in {"batch", "manual", "editor", "review"}:
            raise ValueError(f"Unsupported render reason: {self.reason}")
