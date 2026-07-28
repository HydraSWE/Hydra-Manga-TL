"""Canonical page-level pipeline object for Hydra v0.8 artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class IntelligentPageResult:
    pipeline_version: str
    image_id: str
    source: str
    target_language: str
    preprocessing: dict[str, Any] = field(default_factory=dict)
    ocr_attempts: dict[str, Any] = field(default_factory=dict)
    layout_graph: dict[str, Any] = field(default_factory=dict)
    bubble_segmentation: list[dict[str, Any]] = field(default_factory=list)
    translation_units: list[dict[str, Any]] = field(default_factory=list)
    render_review: dict[str, Any] = field(default_factory=dict)
    debug_artifacts: dict[str, str] = field(default_factory=dict)
    timing: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

