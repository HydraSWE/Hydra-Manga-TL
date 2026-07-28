"""Contracts for semantic title reconstruction providers and reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
from PIL import Image


RECONSTRUCTION_VERSION = "title-reconstruction-v2.5"


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_segmentation: bool = False
    supports_cleanup: bool = False
    supports_background_recovery: bool = False
    supports_confidence: bool = False
    supports_validation: bool = False

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


@dataclass
class ReconstructionTrace:
    steps: list[dict[str, Any]] = field(default_factory=list)

    def add(self, stage: str, status: str, **metadata: Any) -> None:
        self.steps.append({
            "stage": stage,
            "status": status,
            **{key: value for key, value in metadata.items() if value is not None},
        })

    def to_list(self) -> list[dict[str, Any]]:
        return list(self.steps)


@dataclass
class ReconstructionHistory:
    attempts: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_group(cls, group: dict[str, Any]) -> "ReconstructionHistory":
        existing = group.get("reconstruction_history")
        if isinstance(existing, list):
            return cls([dict(item) for item in existing if isinstance(item, dict)])
        return cls()

    def add_attempt(self, **metadata: Any) -> None:
        self.attempts.append({key: value for key, value in metadata.items() if value is not None})

    def to_list(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.attempts]


@dataclass
class TitleReconstructionResult:
    provider: str
    capabilities: ProviderCapabilities
    mask: np.ndarray | None = None
    cleaned_image: Image.Image | None = None
    confidence: float = 0.0
    mask_quality: float = 0.0
    background_quality: float = 0.0
    cleanup_quality: float = 0.0
    method: str = ""
    warning: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def has_mask(self) -> bool:
        return self.mask is not None and np.count_nonzero(self.mask) > 0


@dataclass
class TitleReconstructionReport:
    group: Any
    provider: str
    capabilities: ProviderCapabilities
    status: str
    confidence: float
    mask_quality: float
    background_quality: float
    cleanup_quality: float
    cleanup_method: str
    validation: dict[str, Any] = field(default_factory=dict)
    warning: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    trace: ReconstructionTrace = field(default_factory=ReconstructionTrace)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "group": self.group,
            "title_reconstruction_version": RECONSTRUCTION_VERSION,
            "title_reconstruction_provider": self.provider,
            "title_reconstruction_provider_capabilities": self.capabilities.to_dict(),
            "title_reconstruction_status": self.status,
            "title_reconstruction_confidence": round(float(self.confidence), 3),
            "mask_quality": round(float(self.mask_quality), 3),
            "background_quality": round(float(self.background_quality), 3),
            "cleanup_quality": round(float(self.cleanup_quality), 3),
            "cleanup_method": self.cleanup_method,
            "validation": self.validation,
            "reconstruction_trace": self.trace.to_list(),
        }
        if self.warning:
            payload["title_reconstruction_warning"] = self.warning
        payload.update(self.metadata)
        return payload


@dataclass
class PageTitleReconstruction:
    image: Image.Image
    mask: np.ndarray
    cleaning_method: str
    reports: list[dict[str, Any]]
    title_mask_reports: list[dict[str, Any]]
    background_plate_reports: list[dict[str, Any]]
    accepted_count: int
    needs_review: bool
    inpaint_warning: str = ""
