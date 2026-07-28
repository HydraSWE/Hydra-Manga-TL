"""Versioned reconstruction providers for title artwork recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from PIL import Image

from ..mask_extractor import extract_title_glyph_mask
from .models import ProviderCapabilities, TitleReconstructionResult


class BaseReconstructionProviderV1(Protocol):
    provider_id: str
    label: str
    capabilities: ProviderCapabilities

    def reconstruct_title(
        self,
        image: Image.Image,
        group: dict[str, Any],
        image_size: tuple[int, int],
    ) -> TitleReconstructionResult:
        ...


@dataclass(frozen=True)
class ProviderRegistration:
    key: str
    label: str
    factory: type[BaseReconstructionProviderV1]


class OpenCVReconstructionProvider:
    provider_id = "opencv"
    label = "OpenCV"
    capabilities = ProviderCapabilities(
        supports_segmentation=True,
        supports_cleanup=False,
        supports_background_recovery=False,
        supports_confidence=True,
        supports_validation=False,
    )

    def reconstruct_title(
        self,
        image: Image.Image,
        group: dict[str, Any],
        image_size: tuple[int, int],
    ) -> TitleReconstructionResult:
        glyph = extract_title_glyph_mask(image, group, image_size)
        report = glyph.report()
        return TitleReconstructionResult(
            provider=self.provider_id,
            capabilities=self.capabilities,
            mask=glyph.mask,
            confidence=glyph.confidence,
            mask_quality=glyph.confidence if glyph.accepted else 0.0,
            method=glyph.method,
            warning=glyph.warning,
            metadata=report,
        )


RECONSTRUCTION_PROVIDER_REGISTRY: dict[str, ProviderRegistration] = {
    "opencv": ProviderRegistration("opencv", "OpenCV", OpenCVReconstructionProvider),
}


def create_reconstruction_provider(provider: str) -> tuple[BaseReconstructionProviderV1, str]:
    normalized = (provider or "opencv").strip().lower()
    registration = RECONSTRUCTION_PROVIDER_REGISTRY.get(normalized)
    warning = ""
    if registration is None:
        warning = f"unknown_provider:{normalized or 'empty'}"
        registration = RECONSTRUCTION_PROVIDER_REGISTRY["opencv"]
    return registration.factory(), warning
