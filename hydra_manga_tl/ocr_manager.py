"""Smart OCR orchestration and retry metadata for the v0.8 pipeline."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
import time
from typing import Any, Callable

from PIL import Image

from .language import LanguageEvidence, detect_language, script_fit
from .ocr import OCRResult, clean_ocr_text, ocr_text_quality


class OCRRetryReason(StrEnum):
    LOW_CONFIDENCE = "low_confidence"
    SUSPICIOUS_DIGITS = "suspicious_digits"
    SHORT_VERTICAL_TEXT = "short_vertical_text"
    WRONG_SCRIPT = "wrong_script"
    TINY_TEXT = "tiny_text"


@dataclass(frozen=True)
class OCRAttempt:
    region_id: str
    reason: str
    rect: list[int]
    preferred_language: str
    engine_variant: str
    text: str
    confidence: float
    scripts: dict[str, int]
    accepted: bool
    score_delta: float
    runtime_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ManagedOCRResult:
    ocr_result: OCRResult
    final_regions: list[dict[str, Any]]
    page_language: dict[str, Any]
    region_languages: list[dict[str, Any]]
    attempts: list[OCRAttempt] = field(default_factory=list)
    retry_summary: dict[str, Any] = field(default_factory=dict)
    review_queue: list[dict[str, Any]] = field(default_factory=list)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "page_language": self.page_language,
            "region_languages": self.region_languages,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "retry_summary": self.retry_summary,
            "review_queue": self.review_queue,
        }


EngineFactory = Callable[[tuple[str, ...]], Any]


def _box_from_polygon(polygon: list[list[int]]) -> tuple[int, int, int, int]:
    xs = [int(point[0]) for point in polygon]
    ys = [int(point[1]) for point in polygon]
    return min(xs), min(ys), max(xs), max(ys)


def _polygon_overlap(first: list[list[int]], second: list[list[int]]) -> float:
    ax1, ay1, ax2, ay2 = _box_from_polygon(first)
    bx1, by1, bx2, by2 = _box_from_polygon(second)
    intersection = max(0, min(ax2, bx2) - max(ax1, bx1)) * max(0, min(ay2, by2) - max(ay1, by1))
    first_area = max(1, (ax2 - ax1) * (ay2 - ay1))
    second_area = max(1, (bx2 - bx1) * (by2 - by1))
    return intersection / min(first_area, second_area)


def _language_dict(evidence: LanguageEvidence) -> dict[str, Any]:
    return {
        "language": evidence.language,
        "confidence": evidence.confidence,
        "scripts": evidence.scripts,
    }


class SmartOCRManager:
    """Own OCR retry policy while keeping PaddleOCR as the low-level adapter."""

    def __init__(self, ocr_engine: Any, engine_factory: EngineFactory | None = None) -> None:
        self.ocr_engine = ocr_engine
        self.engine_factory = engine_factory

    def analyze_page(
        self,
        image_path: Path,
        *,
        preferred_language: str | None = None,
        quality: str = "Balanced",
        auto_language_fallback: bool = False,
    ) -> ManagedOCRResult:
        retry_budget = self.retry_budget_for_quality(quality)
        engine = self.ocr_engine
        ocr_result = engine.analyze(image_path, preferred_language)
        attempts: list[OCRAttempt] = []

        if (
            quality == "Maximum"
            and retry_budget > 0
            and auto_language_fallback
            and preferred_language
            and self.engine_factory
            and self._needs_auto_fallback(ocr_result)
        ):
            attempt_started = time.perf_counter()
            fallback = self.engine_factory(("japan", "ch", "en"))
            fallback_result = fallback.analyze(image_path)
            accepted = self._page_score(fallback_result) > self._page_score(ocr_result) + 0.02
            attempts.append(self._page_attempt(
                OCRRetryReason.WRONG_SCRIPT,
                preferred_language,
                fallback_result,
                accepted,
                self._page_score(fallback_result) - self._page_score(ocr_result),
                time.perf_counter() - attempt_started,
            ))
            if accepted:
                engine = fallback
                ocr_result = fallback_result

        final_regions = self._normalized_regions(ocr_result)
        remaining_budget = max(0, retry_budget - len(attempts))
        if remaining_budget:
            final_regions, retry_attempts = self._retry_regions(
                engine, image_path, final_regions, ocr_result.model_language,
                retry_budget=remaining_budget,
                retry_area_limit=self.retry_area_limit_for_quality(quality),
            )
            attempts.extend(retry_attempts)

        return self._managed_result(ocr_result, final_regions, attempts, retry_budget=retry_budget)

    def analyze_selection(
        self,
        image_path: Path,
        rect: list[int],
        *,
        preferred_language: str | None = None,
        quality: str = "Balanced",
    ) -> ManagedOCRResult:
        ocr_result = self.ocr_engine.analyze_selection(image_path, rect, preferred_language=preferred_language)
        final_regions = self._normalized_regions(ocr_result)
        attempts: list[OCRAttempt] = []
        # The user-drawn box is already a focused OCR request. Never turn it
        # into another nested retry; uncertain output goes directly to Review.
        return self._managed_result(ocr_result, final_regions, attempts, retry_budget=0)

    def _managed_result(
        self, ocr_result: OCRResult, final_regions: list[dict[str, Any]], attempts: list[OCRAttempt],
        *, retry_budget: int,
    ) -> ManagedOCRResult:
        combined_text = "\n".join(str(region.get("text", "")) for region in final_regions)
        page_evidence = detect_language(combined_text)
        region_languages = []
        for index, region in enumerate(final_regions, 1):
            evidence = detect_language(str(region.get("text", "")))
            region_languages.append({
                "region_id": f"r{index}",
                **_language_dict(evidence),
            })
            region["language"] = evidence.language
            region["language_confidence"] = evidence.confidence
            region["language_scripts"] = evidence.scripts
            region.setdefault("ocr_review_reasons", [
                reason.value for reason in self._review_reasons(region, ocr_result.model_language)
            ])

        average = sum(float(region.get("confidence", 0.0)) for region in final_regions) / len(final_regions) if final_regions else 0.0
        ocr_result.regions = OCRResult.from_dict({**ocr_result.to_dict(), "regions": final_regions}).regions
        ocr_result.language = page_evidence.language
        ocr_result.language_confidence = page_evidence.confidence
        ocr_result.average_ocr_confidence = average
        ocr_result.language_scripts = page_evidence.scripts

        summary = self._retry_summary(attempts)
        summary.update({
            "budget": retry_budget,
            "remaining": max(0, retry_budget - len(attempts)),
            "budget_exhausted": retry_budget > 0 and len(attempts) >= retry_budget,
        })
        managed = ManagedOCRResult(
            ocr_result=ocr_result,
            final_regions=final_regions,
            page_language=_language_dict(page_evidence),
            region_languages=region_languages,
            attempts=attempts,
            retry_summary=summary,
            review_queue=[
                {
                    "region_id": f"r{index}",
                    "reasons": list(region.get("ocr_review_reasons", [])),
                    "text": str(region.get("text", "")),
                    "confidence": float(region.get("confidence", 0.0)),
                }
                for index, region in enumerate(final_regions, 1)
                if region.get("ocr_review_reasons")
            ],
        )
        ocr_result.metadata = {**ocr_result.metadata, "manager": managed.to_metadata()}
        return managed

    def _retry_regions(
        self,
        engine: Any,
        image_path: Path,
        regions: list[dict[str, Any]],
        model_language: str,
        *,
        selection_rect: list[int] | None = None,
        retry_budget: int,
        retry_area_limit: int,
    ) -> tuple[list[dict[str, Any]], list[OCRAttempt]]:
        attempts: list[OCRAttempt] = []
        entries = [{**region, "text": clean_ocr_text(str(region.get("text", "")))} for region in regions]
        image_size = self._image_size(image_path)
        ranked: list[tuple[float, int, OCRRetryReason]] = []
        for index, entry in enumerate(entries):
            reasons = self._retry_reasons(entry, model_language)
            if reasons:
                reason = self._primary_reason(reasons)
                ranked.append((self._retry_priority_score(entry, reason, model_language), index, reason))

        ordered = sorted(ranked, key=lambda item: (-item[0], item[1]))
        eligible: list[tuple[float, int, OCRRetryReason, list[int]]] = []
        for score, index, reason in ordered:
            rect = selection_rect or self._retry_rect(entries[index], reason, image_size)
            area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
            if area > retry_area_limit:
                entries[index]["ocr_review_reasons"] = [
                    "retry_deferred:oversized_crop",
                    *[f"retry_deferred:{item.value}" for item in self._review_reasons(entries[index], model_language)],
                ]
                continue
            eligible.append((score, index, reason, rect))

        selected = {index for _, index, _, _ in eligible[:retry_budget]}
        for _, index, reason, _ in eligible:
            if index not in selected:
                entries[index]["ocr_review_reasons"] = [
                    f"retry_deferred:{item.value}"
                    for item in self._review_reasons(entries[index], model_language)
                ]

        for _, index, reason, rect in eligible[:retry_budget]:
            entry = entries[index]
            attempt_started = time.perf_counter()
            retry = engine.analyze_selection(
                image_path,
                rect,
                preferred_language=model_language,
                add_context=False,
                rtl_context=False,
            )
            candidates = self._normalized_regions(retry)
            best = self._best_candidate(entry, candidates, model_language)
            original_score = self._region_quality(str(entry.get("text", "")), float(entry.get("confidence", 0.0)), model_language)
            candidate_score = (
                self._region_quality(str(best.get("text", "")), float(best.get("confidence", 0.0)), model_language)
                if best else original_score
            )
            accepted = best is not None and candidate_score > original_score + 0.02
            attempts.append(self._region_attempt(
                f"r{index + 1}", reason, rect, model_language, best, accepted, candidate_score - original_score,
                time.perf_counter() - attempt_started,
            ))
            if accepted and best is not None:
                entries[index].update(best)
            unresolved = self._review_reasons(entries[index], model_language)
            entries[index]["ocr_review_reasons"] = [
                (f"retry_unresolved:{item.value}" if accepted else f"retry_rejected:{item.value}")
                for item in unresolved
            ]

        return entries, attempts

    @staticmethod
    def retry_budget_for_quality(quality: str) -> int:
        return {"Fast": 0, "Balanced": 1, "Maximum": 3}.get(str(quality), 1)

    @staticmethod
    def retry_area_limit_for_quality(quality: str) -> int:
        return {"Fast": 0, "Balanced": 75_000, "Maximum": 200_000}.get(str(quality), 75_000)

    @staticmethod
    def _retry_priority_score(
        region: dict[str, Any], reason: OCRRetryReason, model_language: str,
    ) -> float:
        confidence = float(region.get("confidence", 0.0))
        text = str(region.get("text", ""))
        quality = SmartOCRManager._region_quality(text, confidence, model_language)
        reason_weight = {
            OCRRetryReason.SUSPICIOUS_DIGITS: 0.50,
            OCRRetryReason.WRONG_SCRIPT: 0.45,
            OCRRetryReason.SHORT_VERTICAL_TEXT: 0.35,
            OCRRetryReason.LOW_CONFIDENCE: 0.25,
            OCRRetryReason.TINY_TEXT: 0.10,
        }[reason]
        return (1.0 - max(0.0, min(1.0, confidence))) * 0.60 + (1.0 - quality) * 0.40 + reason_weight

    @staticmethod
    def _normalized_regions(ocr_result: OCRResult) -> list[dict[str, Any]]:
        regions = []
        for region in ocr_result.to_dict().get("regions", []):
            text = clean_ocr_text(str(region.get("text", "")))
            if not text.strip():
                continue
            regions.append({
                "text": text,
                "confidence": float(region.get("confidence", 0.0)),
                "polygon": [[int(point[0]), int(point[1])] for point in region.get("polygon", [])],
            })
        return regions

    @staticmethod
    def _needs_auto_fallback(ocr_result: OCRResult) -> bool:
        if ocr_result.language != "unknown" and ocr_result.language_confidence >= 0.70:
            return False
        return (
            ocr_result.average_ocr_confidence < 0.50
            or ocr_result.language_confidence < 0.55
            or ocr_result.language == "unknown"
        )

    @staticmethod
    def _page_score(ocr_result: OCRResult) -> float:
        text = "\n".join(region.text for region in ocr_result.regions)
        readable = min(len("".join(text.split())) / 30.0, 1.0)
        return ocr_result.average_ocr_confidence * 0.65 + ocr_result.language_confidence * 0.25 + readable * 0.10

    @staticmethod
    def _retry_reasons(region: dict[str, Any], model_language: str) -> list[OCRRetryReason]:
        text = str(region.get("text", ""))
        confidence = float(region.get("confidence", 0.0))
        x1, y1, x2, y2 = _box_from_polygon(region.get("polygon", [[0, 0], [0, 0], [0, 0], [0, 0]]))
        width, height = max(1, x2 - x1), max(1, y2 - y1)
        compact = "".join(text.split())
        reasons: list[OCRRetryReason] = []
        if confidence <= 0.45:
            reasons.append(OCRRetryReason.LOW_CONFIDENCE)
        if sum(char.isascii() and char.isdigit() for char in text) >= 3:
            reasons.append(OCRRetryReason.SUSPICIOUS_DIGITS)
        if height > width * 1.5 and len(compact) <= 2 and confidence < 0.55:
            reasons.append(OCRRetryReason.SHORT_VERTICAL_TEXT)
        if compact and (script_fit(text, model_language) < 0.25 or SmartOCRManager._wrong_script(text, model_language)):
            reasons.append(OCRRetryReason.WRONG_SCRIPT)
        return list(dict.fromkeys(reasons))

    @staticmethod
    def _review_reasons(region: dict[str, Any], model_language: str) -> list[OCRRetryReason]:
        text = str(region.get("text", ""))
        confidence = float(region.get("confidence", 0.0))
        x1, y1, x2, y2 = _box_from_polygon(region.get("polygon", [[0, 0], [0, 0], [0, 0], [0, 0]]))
        width, height = max(1, x2 - x1), max(1, y2 - y1)
        compact = "".join(text.split())
        reasons: list[OCRRetryReason] = []
        if confidence < 0.70:
            reasons.append(OCRRetryReason.LOW_CONFIDENCE)
        if sum(char.isascii() and char.isdigit() for char in text) >= 3:
            reasons.append(OCRRetryReason.SUSPICIOUS_DIGITS)
        if height > width * 1.5 and len(compact) <= 3:
            reasons.append(OCRRetryReason.SHORT_VERTICAL_TEXT)
        if compact and (script_fit(text, model_language) < 0.45 or SmartOCRManager._wrong_script(text, model_language)):
            reasons.append(OCRRetryReason.WRONG_SCRIPT)
        if min(width, height) <= 8 or (width * height <= 96 and confidence < 0.90):
            reasons.append(OCRRetryReason.TINY_TEXT)
        return list(dict.fromkeys(reasons))

    @staticmethod
    def _primary_reason(reasons: list[OCRRetryReason]) -> OCRRetryReason:
        priority = [
            OCRRetryReason.SUSPICIOUS_DIGITS,
            OCRRetryReason.SHORT_VERTICAL_TEXT,
            OCRRetryReason.WRONG_SCRIPT,
            OCRRetryReason.LOW_CONFIDENCE,
            OCRRetryReason.TINY_TEXT,
        ]
        return next(reason for reason in priority if reason in reasons)

    @staticmethod
    def _wrong_script(text: str, model_language: str) -> bool:
        evidence = detect_language(text)
        scripts = evidence.scripts
        cjk_count = scripts.get("kana", 0) + scripts.get("han", 0) + scripts.get("hangul", 0)
        latin_count = scripts.get("latin", 0)
        if model_language == "japan":
            return cjk_count == 0 and latin_count >= 3
        if model_language == "ch":
            return scripts.get("han", 0) == 0 and latin_count >= 3
        return False

    @staticmethod
    def _image_size(image_path: Path) -> tuple[int, int]:
        with Image.open(image_path) as opened:
            return opened.size

    @staticmethod
    def _retry_rect(region: dict[str, Any], reason: OCRRetryReason, image_size: tuple[int, int]) -> list[int]:
        x1, y1, x2, y2 = _box_from_polygon(region["polygon"])
        width, height = max(1, x2 - x1), max(1, y2 - y1)
        if reason == OCRRetryReason.SHORT_VERTICAL_TEXT:
            left = max(width * 2, 80)
            right = max(width, 40)
            top = 4
            bottom = max(8, height // 4)
        else:
            left = right = max(width, 16)
            top = max(4, height // 3)
            bottom = max(8, height // 2)
        return [
            max(0, x1 - left),
            max(0, y1 - top),
            min(image_size[0], x2 + right),
            min(image_size[1], y2 + bottom),
        ]

    @staticmethod
    def _best_candidate(
        original: dict[str, Any], candidates: list[dict[str, Any]], model_language: str,
    ) -> dict[str, Any] | None:
        if not candidates:
            return None
        overlapping = [
            candidate for candidate in candidates
            if _polygon_overlap(original["polygon"], candidate["polygon"]) >= 0.10
        ]
        pool = overlapping or candidates
        return max(
            pool,
            key=lambda candidate: ocr_text_quality(
                str(candidate.get("text", "")), float(candidate.get("confidence", 0.0)), model_language,
            ) - (0.20 if SmartOCRManager._wrong_script(str(candidate.get("text", "")), model_language) else 0.0),
        )

    @staticmethod
    def _region_quality(text: str, confidence: float, model_language: str) -> float:
        score = ocr_text_quality(text, confidence, model_language)
        if SmartOCRManager._wrong_script(text, model_language):
            score -= 0.20
        return score

    @staticmethod
    def _region_attempt(
        region_id: str,
        reason: OCRRetryReason,
        rect: list[int],
        model_language: str,
        candidate: dict[str, Any] | None,
        accepted: bool,
        score_delta: float,
        runtime_seconds: float,
    ) -> OCRAttempt:
        text = str(candidate.get("text", "")) if candidate else ""
        confidence = float(candidate.get("confidence", 0.0)) if candidate else 0.0
        evidence = detect_language(text)
        return OCRAttempt(
            region_id=region_id,
            reason=reason.value,
            rect=[int(value) for value in rect],
            preferred_language=model_language,
            engine_variant="focused_color_2x",
            text=text,
            confidence=confidence,
            scripts=evidence.scripts,
            accepted=accepted,
            score_delta=round(score_delta, 4),
            runtime_seconds=round(runtime_seconds, 4),
        )

    @staticmethod
    def _page_attempt(
        reason: OCRRetryReason, preferred_language: str, result: OCRResult, accepted: bool, score_delta: float,
        runtime_seconds: float,
    ) -> OCRAttempt:
        evidence = detect_language("\n".join(region.text for region in result.regions))
        return OCRAttempt(
            region_id="page",
            reason=reason.value,
            rect=[],
            preferred_language=preferred_language,
            engine_variant="auto_language_fallback",
            text="\n".join(region.text for region in result.regions),
            confidence=result.average_ocr_confidence,
            scripts=evidence.scripts,
            accepted=accepted,
            score_delta=round(score_delta, 4),
            runtime_seconds=round(runtime_seconds, 4),
        )

    @staticmethod
    def _retry_summary(attempts: list[OCRAttempt]) -> dict[str, Any]:
        by_reason = Counter(attempt.reason for attempt in attempts)
        runtime_by_reason: dict[str, list[float]] = {}
        delta_by_reason: dict[str, list[float]] = {}
        for attempt in attempts:
            runtime_by_reason.setdefault(attempt.reason, []).append(attempt.runtime_seconds)
            delta_by_reason.setdefault(attempt.reason, []).append(attempt.score_delta)
        return {
            "attempt_count": len(attempts),
            "accepted_count": sum(1 for attempt in attempts if attempt.accepted),
            "rejected_count": sum(1 for attempt in attempts if not attempt.accepted),
            "by_reason": dict(sorted(by_reason.items())),
            "reason_stats": {
                reason: {
                    "attempts": by_reason[reason],
                    "accepted": sum(1 for attempt in attempts if attempt.reason == reason and attempt.accepted),
                    "rejected": sum(1 for attempt in attempts if attempt.reason == reason and not attempt.accepted),
                    "average_score_delta": round(sum(delta_by_reason[reason]) / len(delta_by_reason[reason]), 4),
                    "average_runtime_seconds": round(sum(runtime_by_reason[reason]) / len(runtime_by_reason[reason]), 4),
                }
                for reason in sorted(by_reason)
            },
        }
