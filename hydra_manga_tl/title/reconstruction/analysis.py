"""Advisory reconstruction analysis providers and hint validation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import base64
from io import BytesIO
import json
import os
from typing import Any, Protocol

from PIL import Image

from hydra_manga_tl.core.settings import CREDENTIALS, SETTINGS
from hydra_manga_tl.translation.service import _post_json
from hydra_manga_tl.translation.engines.qwen_engine import extract_first_json_object
from ..mask_extractor import _combined_box, _normalize_polygons


TRUST_IGNORED = 0
TRUST_OBSERVED = 1
TRUST_SUGGESTED = 2
TRUST_VALIDATED = 3
TRUST_APPLIED = 4


@dataclass(frozen=True)
class ReconstructionAnalysisCapabilities:
    supports_geometry: bool = False
    supports_hierarchy: bool = False
    supports_mask_hints: bool = False
    supports_background_analysis: bool = False
    supports_cleanup_review: bool = False
    supports_style_analysis: bool = False

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


@dataclass
class ReconstructionRequest:
    crop: Image.Image
    image_size: tuple[int, int]
    group: dict[str, Any]
    source_polygons: list[list[list[int]]]
    ocr_text: str
    source_text_colors: list[Any] = field(default_factory=list)
    existing_masks: dict[str, Any] = field(default_factory=dict)
    style_profile: dict[str, Any] = field(default_factory=dict)
    reconstruction_history: list[dict[str, Any]] = field(default_factory=list)
    image_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconstructionAnalysisResult:
    provider: str
    capabilities: ReconstructionAnalysisCapabilities
    confidence: float = 0.0
    hints: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "capabilities": self.capabilities.to_dict(),
            "confidence": round(float(self.confidence), 3),
            "hints": deepcopy(self.hints),
            "warnings": list(self.warnings),
            "metadata": deepcopy(self.metadata),
        }


class BaseReconstructionAnalysisProviderV1(Protocol):
    provider_id: str
    label: str
    capabilities: ReconstructionAnalysisCapabilities

    def analyze_reconstruction(self, request: ReconstructionRequest) -> ReconstructionAnalysisResult:
        ...


@dataclass(frozen=True)
class AnalysisProviderRegistration:
    key: str
    label: str
    factory: type[BaseReconstructionAnalysisProviderV1]


class NoneReconstructionAnalysisProvider:
    provider_id = "none"
    label = "None"
    capabilities = ReconstructionAnalysisCapabilities()

    def analyze_reconstruction(self, request: ReconstructionRequest) -> ReconstructionAnalysisResult:
        return ReconstructionAnalysisResult(provider=self.provider_id, capabilities=self.capabilities)


class RemoteReconstructionAnalysisProvider:
    provider_id = ""
    label = ""
    endpoint = ""
    default_model = ""
    capabilities = ReconstructionAnalysisCapabilities(
        supports_geometry=True,
        supports_mask_hints=True,
        supports_background_analysis=True,
        supports_cleanup_review=True,
        supports_style_analysis=True,
    )

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else CREDENTIALS.get(self.provider_id)
        self.model = (
            model
            or os.environ.get(f"HYDRA_RECONSTRUCTION_{self.provider_id.upper()}_MODEL", "").strip()
            or self.default_model
        )

    def analyze_reconstruction(self, request: ReconstructionRequest) -> ReconstructionAnalysisResult:
        if not self.api_key:
            return ReconstructionAnalysisResult(
                provider=self.provider_id,
                capabilities=self.capabilities,
                confidence=0.0,
                warnings=[f"{self.provider_id}_api_key_not_configured"],
            )
        if not self.model:
            return ReconstructionAnalysisResult(
                provider=self.provider_id,
                capabilities=self.capabilities,
                confidence=0.0,
                warnings=[f"{self.provider_id}_model_not_configured"],
            )
        try:
            payload = self._request_payload(request)
            response = _post_json(self._endpoint(), payload, headers=self._headers())
            raw = self._raw_text(response)
            parsed = extract_first_json_object(raw)
            hints = parsed.get("hints", [])
            if not isinstance(hints, list):
                hints = []
            confidence = float(parsed.get("confidence", 0.0) or 0.0)
            warnings = parsed.get("warnings", [])
            if not isinstance(warnings, list):
                warnings = [str(warnings)]
            return ReconstructionAnalysisResult(
                provider=self.provider_id,
                capabilities=self.capabilities,
                confidence=max(0.0, min(1.0, confidence)),
                hints=[item for item in hints if isinstance(item, dict)],
                warnings=[str(item) for item in warnings],
                metadata={
                    "model": self.model,
                    "raw_hint_count": len(hints),
                },
            )
        except Exception as error:
            detail = str(error)[:240]
            return ReconstructionAnalysisResult(
                provider=self.provider_id,
                capabilities=self.capabilities,
                confidence=0.0,
                warnings=[f"{self.provider_id}_analysis_error:{type(error).__name__}"],
                metadata={"model": self.model, "error": detail},
            )

    def _endpoint(self) -> str:
        return self.endpoint

    def _headers(self) -> dict[str, str] | None:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _request_payload(self, request: ReconstructionRequest) -> dict[str, Any]:
        raise NotImplementedError

    def _raw_text(self, response: dict[str, Any]) -> str:
        return str(response["choices"][0]["message"]["content"])

    def _encoded_crop(self, request: ReconstructionRequest) -> str:
        buffer = BytesIO()
        request.crop.convert("RGB").save(buffer, format="JPEG", quality=90)
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    def _prompt(self, request: ReconstructionRequest) -> str:
        group = request.group
        source_box = _combined_box(request.source_polygons, request.image_size, pad=8)
        return (
            "You are an advisory title reconstruction analyzer for Hydra Manga TL. "
            "Return JSON only. You do not decide final cleanup. Hydra will validate every hint. "
            "Analyze the attached crop and metadata for decorative manga title removal.\n\n"
            "Return this shape exactly:\n"
            "{"
            "\"confidence\":0.0,"
            "\"warnings\":[],"
            "\"hints\":["
            "{\"type\":\"cleanup_polygon\",\"polygon\":[[x,y],[x,y],[x,y],[x,y]],\"confidence\":0.0,\"reason\":\"tight glyph/title pixels only\"},"
            "{\"type\":\"background_risk\",\"confidence\":0.0,\"reason\":\"face_overlap|hair_overlap|complex_art|simple_background\"}"
            "]"
            "}\n\n"
            "Rules:\n"
            "- Coordinates must be absolute full-image pixel coordinates, not crop-relative.\n"
            "- Suggest cleanup polygons only around printed title glyphs or tight title clusters, never a whole rectangle if it includes character art.\n"
            "- Add character/background risk hints when title cleanup may touch faces, hands, hair, clothing, or detailed line art.\n"
            "- If uncertain, return low confidence and warnings; do not invent exact masks.\n\n"
            f"image_size={request.image_size}\n"
            f"crop_box={request.image_metadata.get('crop_box')}\n"
            f"title_source_box={source_box}\n"
            f"source_polygons={json.dumps(request.source_polygons, ensure_ascii=False)}\n"
            f"ocr_text={request.ocr_text!r}\n"
            f"source_text_colors={json.dumps(request.source_text_colors, ensure_ascii=False)}\n"
            f"style_profile={json.dumps(request.style_profile, ensure_ascii=False)}\n"
            f"translated_text={str(group.get('translated_text') or '')!r}\n"
        )


class GroqReconstructionAnalysisProvider(RemoteReconstructionAnalysisProvider):
    provider_id = "groq"
    label = "Groq Vision"
    endpoint = "https://api.groq.com/openai/v1/chat/completions"
    default_model = "meta-llama/llama-4-scout-17b-16e-instruct"

    def _request_payload(self, request: ReconstructionRequest) -> dict[str, Any]:
        image_url = f"data:image/jpeg;base64,{self._encoded_crop(request)}"
        return {
            "model": self.model,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": "Return strict JSON only."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._prompt(request)},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ],
            "response_format": {"type": "json_object"},
        }


class GeminiReconstructionAnalysisProvider(RemoteReconstructionAnalysisProvider):
    provider_id = "gemini"
    label = "Gemini Vision"
    default_model = "gemini-3.5-flash"

    def _endpoint(self) -> str:
        from urllib.parse import quote

        return f"https://generativelanguage.googleapis.com/v1beta/models/{quote(self.model)}:generateContent?key={quote(self.api_key)}"

    def _headers(self) -> dict[str, str] | None:
        return None

    def _request_payload(self, request: ReconstructionRequest) -> dict[str, Any]:
        return {
            "contents": [{
                "parts": [
                    {"text": self._prompt(request)},
                    {"inlineData": {"mimeType": "image/jpeg", "data": self._encoded_crop(request)}},
                ],
            }],
            "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
        }

    def _raw_text(self, response: dict[str, Any]) -> str:
        return str(response["candidates"][0]["content"]["parts"][0]["text"])


RECONSTRUCTION_ANALYSIS_PROVIDER_REGISTRY: dict[str, AnalysisProviderRegistration] = {
    "none": AnalysisProviderRegistration("none", "None", NoneReconstructionAnalysisProvider),
    "groq": AnalysisProviderRegistration("groq", "Groq Vision", GroqReconstructionAnalysisProvider),
    "gemini": AnalysisProviderRegistration("gemini", "Gemini Vision", GeminiReconstructionAnalysisProvider),
}


def create_reconstruction_analysis_provider(provider: str) -> tuple[BaseReconstructionAnalysisProviderV1, str]:
    normalized = (provider or "none").strip().lower()
    registration = RECONSTRUCTION_ANALYSIS_PROVIDER_REGISTRY.get(normalized)
    warning = ""
    if registration is None:
        warning = f"unknown_analysis_provider:{normalized or 'empty'}"
        registration = RECONSTRUCTION_ANALYSIS_PROVIDER_REGISTRY["none"]
    return registration.factory(), warning


def build_reconstruction_request(image: Image.Image, group: dict[str, Any], image_size: tuple[int, int]) -> ReconstructionRequest:
    source_polygons = _normalize_polygons(group.get("source_polygons") or [group.get("polygon", [])])
    box = _combined_box(source_polygons, image_size, pad=8) or [0, 0, image_size[0], image_size[1]]
    crop = image.crop(tuple(box)).convert("RGB")
    return ReconstructionRequest(
        crop=crop,
        image_size=image_size,
        group=deepcopy(group),
        source_polygons=deepcopy(source_polygons),
        ocr_text=str(group.get("original_text") or group.get("text") or ""),
        source_text_colors=deepcopy(group.get("source_text_colors") or group.get("source_member_colors") or []),
        existing_masks=deepcopy({
            "mask_polygons": group.get("mask_polygons"),
            "cleanup_polygons": group.get("cleanup_polygons"),
        }),
        style_profile=deepcopy(group.get("style_profile") if isinstance(group.get("style_profile"), dict) else {}),
        reconstruction_history=deepcopy(group.get("reconstruction_history") if isinstance(group.get("reconstruction_history"), list) else []),
        image_metadata={"crop_box": box},
    )


def _polygon_inside(polygon: Any, source_box: list[int]) -> bool:
    normalized = _normalize_polygons(polygon)
    if not normalized:
        return False
    sx1, sy1, sx2, sy2 = source_box
    for item in normalized:
        for x, y in item:
            if x < sx1 or x > sx2 or y < sy1 or y > sy2:
                return False
    return True


def _polygon_area_ratio(polygons: Any, source_box: list[int]) -> float:
    normalized = _normalize_polygons(polygons)
    if not normalized:
        return 0.0
    source_area = max(1, (source_box[2] - source_box[0]) * (source_box[3] - source_box[1]))
    area = 0.0
    for polygon in normalized:
        if len(polygon) < 3:
            continue
        points = polygon + [polygon[0]]
        signed = 0.0
        for first, second in zip(points, points[1:]):
            signed += first[0] * second[1] - second[0] * first[1]
        area += abs(signed) / 2.0
    return float(area / source_area)


def validate_reconstruction_hints(
    analysis: ReconstructionAnalysisResult,
    request: ReconstructionRequest,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    source_box = _combined_box(request.source_polygons, request.image_size, pad=8) or [0, 0, *request.image_size]
    validated: list[dict[str, Any]] = []
    ignored: list[str] = []
    used: list[str] = []
    for index, hint in enumerate(analysis.hints):
        item = deepcopy(hint) if isinstance(hint, dict) else {"value": hint}
        hint_type = str(item.get("type") or "unknown")
        confidence = float(item.get("confidence", analysis.confidence or 0.0) or 0.0)
        trust = TRUST_OBSERVED
        reason = ""
        if confidence < 0.35:
            trust = TRUST_OBSERVED
            reason = "low_confidence_observed_only"
        elif hint_type in {"mask_polygon", "geometry", "cleanup_polygon"}:
            polygons = item.get("polygon") or item.get("polygons")
            area_ratio = _polygon_area_ratio(polygons, source_box)
            item["source_area_ratio"] = round(area_ratio, 4)
            if not _polygon_inside(polygons, source_box):
                trust = TRUST_IGNORED
                reason = "hint_outside_title_region"
            elif area_ratio <= 0.0:
                trust = TRUST_IGNORED
                reason = "hint_empty_geometry"
            elif area_ratio > 0.48:
                trust = TRUST_OBSERVED
                reason = "hint_too_broad_observed_only"
            elif confidence >= 0.75:
                trust = TRUST_VALIDATED
                reason = "geometry_validated"
            else:
                trust = TRUST_SUGGESTED
                reason = "geometry_suggested"
        elif hint_type in {"hierarchy", "reading_order", "style"}:
            trust = TRUST_SUGGESTED if confidence >= 0.5 else TRUST_OBSERVED
            reason = "advisory_structure_hint"
        elif hint_type in {"character_overlap", "cleanup_review", "background_risk"}:
            trust = TRUST_VALIDATED if confidence >= 0.65 else TRUST_OBSERVED
            reason = "review_risk_validated" if trust == TRUST_VALIDATED else "review_risk_observed"
        else:
            trust = TRUST_OBSERVED
            reason = "unsupported_hint_recorded"
        item["trust_level"] = trust
        item["trust_label"] = {
            TRUST_IGNORED: "ignored",
            TRUST_OBSERVED: "observed",
            TRUST_SUGGESTED: "suggested",
            TRUST_VALIDATED: "validated",
            TRUST_APPLIED: "applied",
        }[trust]
        item["validation_reason"] = reason
        item["hint_index"] = index
        validated.append(item)
        if trust == TRUST_IGNORED:
            ignored.append(reason)
        elif trust >= TRUST_SUGGESTED:
            used.append(hint_type)
    return validated, list(dict.fromkeys(ignored)), list(dict.fromkeys(used))


def apply_analysis_to_group(group: dict[str, Any], validated_hints: list[dict[str, Any]]) -> list[str]:
    applied: list[str] = []
    for hint in validated_hints:
        if int(hint.get("trust_level", 0)) < TRUST_VALIDATED:
            continue
        hint_type = str(hint.get("type") or "")
        if hint_type in {"character_overlap", "cleanup_review", "background_risk"}:
            review_reasons = list(group.get("reconstruction_review_reasons", []))
            reason = str(hint.get("reason") or hint_type)
            if reason not in review_reasons:
                review_reasons.append(reason)
            group["reconstruction_review_reasons"] = review_reasons
            hint["trust_level"] = TRUST_APPLIED
            hint["trust_label"] = "applied"
            applied.append(hint_type)
        elif hint_type in {"mask_polygon", "geometry", "cleanup_polygon"}:
            metadata = dict(group.get("reconstruction_analysis_metadata", {}))
            metadata.setdefault("validated_geometry_hints", []).append(deepcopy(hint))
            group["reconstruction_analysis_metadata"] = metadata
            polygons = hint.get("polygon") or hint.get("polygons")
            if hint_type == "cleanup_polygon" and polygons:
                existing = _normalize_polygons(group.get("cleanup_polygons"))
                group["cleanup_polygons"] = [*existing, *_normalize_polygons(polygons)]
            hint["trust_level"] = TRUST_APPLIED
            hint["trust_label"] = "applied"
            applied.append(hint_type)
    return list(dict.fromkeys(applied))
