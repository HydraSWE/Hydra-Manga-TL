"""Semantic title reconstruction orchestration for Phase 3 cleanup."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image

from hydra_manga_tl.phase.art_inpaint import InpaintRuntimeUnavailable, clean_art_text_background
from hydra_manga_tl.phase.renderer import clean_background
from ..background_plate import apply_title_background_plates
from .analysis import (
    apply_analysis_to_group,
    build_reconstruction_request,
    create_reconstruction_analysis_provider,
    validate_reconstruction_hints,
)
from .models import (
    PageTitleReconstruction,
    ProviderCapabilities,
    RECONSTRUCTION_VERSION,
    ReconstructionHistory,
    ReconstructionTrace,
    TitleReconstructionReport,
    TitleReconstructionResult,
)
from .providers import create_reconstruction_provider


InpaintResolver = Callable[[], str]


@dataclass
class _CachedTitleReconstruction:
    mask: np.ndarray
    report: dict[str, Any]


_RECONSTRUCTION_CACHE: dict[str, _CachedTitleReconstruction] = {}


def _cache_key(group: dict[str, Any], image_size: tuple[int, int], provider: str, analysis_provider: str) -> str:
    payload = {
        "version": RECONSTRUCTION_VERSION,
        "provider": provider,
        "analysis_provider": analysis_provider,
        "size": list(image_size),
        "project_id": group.get("project_id", ""),
        "original_text": group.get("original_text") or group.get("text") or "",
        "source_polygons": group.get("source_polygons") or [group.get("polygon", [])],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def clear_reconstruction_cache() -> None:
    _RECONSTRUCTION_CACHE.clear()


def _mask_validation(
    mask: np.ndarray | None,
    image_size: tuple[int, int],
    metadata: dict[str, Any],
    *,
    max_coverage: float = 0.18,
) -> dict[str, Any]:
    if mask is None:
        return {"passed": False, "reason": "missing_mask", "coverage": 0.0}
    if mask.shape != (image_size[1], image_size[0]):
        return {"passed": False, "reason": "mask_size_mismatch", "coverage": 0.0}
    coverage = float(np.count_nonzero(mask) / max(1, image_size[0] * image_size[1]))
    warning = str(metadata.get("title_mask_warning") or "")
    passed = bool(np.count_nonzero(mask) > 0 and not warning and coverage <= max_coverage)
    reason = "" if passed else (warning or "mask_not_safe")
    return {"passed": passed, "reason": reason, "coverage": round(coverage, 5)}


def _cleaned_validation(original: Image.Image, cleaned: Image.Image | None, mask: np.ndarray | None) -> dict[str, Any]:
    if cleaned is None:
        return {"passed": False, "reason": "missing_cleaned_image"}
    if cleaned.size != original.size:
        return {"passed": False, "reason": "cleaned_size_mismatch"}
    if mask is None or not np.count_nonzero(mask):
        return {"passed": False, "reason": "missing_mask_for_cleaned_image"}
    width, height = original.size
    difference_sum = 0
    compared_values = 0
    for top in range(0, height, 256):
        bottom = min(height, top + 256)
        outside = mask[top:bottom] <= 0
        outside_pixels = int(np.count_nonzero(outside))
        if not outside_pixels:
            continue
        original_chunk = np.asarray(
            original.crop((0, top, width, bottom)).convert("RGB"),
            dtype=np.int16,
        )
        cleaned_chunk = np.asarray(
            cleaned.crop((0, top, width, bottom)).convert("RGB"),
            dtype=np.int16,
        )
        difference = np.abs(original_chunk - cleaned_chunk)
        difference_sum += int(difference[outside].sum(dtype=np.uint64))
        compared_values += outside_pixels * 3
        del original_chunk, cleaned_chunk, difference
    outside_delta = difference_sum / compared_values if compared_values else 0.0
    passed = outside_delta <= 10.0
    return {
        "passed": passed,
        "reason": "" if passed else "cleaned_changed_unmasked_art",
        "outside_delta": round(outside_delta, 3),
    }


def _score(mask_quality: float, cleanup_quality: float, background_quality: float) -> float:
    return max(mask_quality, min(0.98, mask_quality * 0.55 + cleanup_quality * 0.45), background_quality)


def reconstruct_title_group(
    image: Image.Image,
    group: dict[str, Any],
    image_size: tuple[int, int],
    *,
    provider_id: str = "opencv",
    analysis_provider_id: str = "none",
) -> tuple[TitleReconstructionResult, TitleReconstructionReport]:
    trace = ReconstructionTrace()
    request = build_reconstruction_request(image, group, image_size)
    analysis_provider, analysis_warning = create_reconstruction_analysis_provider(analysis_provider_id)
    analysis_hints: list[dict[str, Any]] = []
    analysis_ignored: list[str] = []
    ai_suggestions_used: list[str] = []
    analysis_confidence = 0.0
    trace.add(
        "reconstruction_analysis",
        "fallback" if analysis_warning else "selected",
        requested=analysis_provider_id,
        selected=analysis_provider.provider_id,
        warning=analysis_warning or None,
        capabilities=analysis_provider.capabilities.to_dict(),
    )
    try:
        analysis = analysis_provider.analyze_reconstruction(request)
        analysis_confidence = float(analysis.confidence or 0.0)
        analysis_hints, analysis_ignored, _suggested = validate_reconstruction_hints(analysis, request)
        trace.add(
            "hint_validation",
            "completed",
            hint_count=len(analysis_hints),
            ignored_reasons=analysis_ignored,
        )
    except Exception as error:
        analysis_hints = []
        analysis_ignored = [f"analysis_error:{type(error).__name__}"]
        trace.add("reconstruction_analysis", "failed", error=type(error).__name__)

    working_group = deepcopy(group)
    ai_suggestions_used = apply_analysis_to_group(working_group, analysis_hints)
    provider, provider_warning = create_reconstruction_provider(provider_id)
    effective_provider = provider.provider_id
    if provider_warning:
        trace.add("provider", "fallback", requested=provider_id, selected=effective_provider, warning=provider_warning)
    else:
        trace.add("provider", "selected", selected=effective_provider)
    trace.add("provider_capabilities", "reported", capabilities=provider.capabilities.to_dict())

    key = _cache_key(working_group, image_size, effective_provider, analysis_provider.provider_id)
    cached = _RECONSTRUCTION_CACHE.get(key)
    if cached is not None:
        cached_report = dict(cached.report)
        trace.add("cache", "hit")
        result = TitleReconstructionResult(
            provider=effective_provider,
            capabilities=provider.capabilities,
            mask=cached.mask.copy(),
            confidence=float(cached_report.get("title_reconstruction_confidence", 0.0) or 0.0),
            mask_quality=float(cached_report.get("mask_quality", 0.0) or 0.0),
            method=str(cached_report.get("title_mask_method") or "opencv-glyph"),
            metadata={**cached_report, "title_reconstruction_cache": "hit"},
        )
        return result, TitleReconstructionReport(
            group=group.get("index"),
            provider=effective_provider,
            capabilities=provider.capabilities,
            status="accepted",
            confidence=result.confidence,
            mask_quality=result.mask_quality,
            background_quality=float(cached_report.get("background_quality", 0.0) or 0.0),
            cleanup_quality=float(cached_report.get("cleanup_quality", 0.0) or 0.0),
            cleanup_method=str(cached_report.get("cleanup_method") or ""),
            validation=dict(cached_report.get("validation", {})),
            metadata={
                **cached_report,
                "title_reconstruction_cache": "hit",
                "reconstruction_analysis_provider": analysis_provider.provider_id,
                "reconstruction_analysis_capabilities": analysis_provider.capabilities.to_dict(),
                "reconstruction_analysis_confidence": round(analysis_confidence, 3),
                "reconstruction_analysis_hints": analysis_hints,
                "reconstruction_analysis_trust_levels": [hint.get("trust_level") for hint in analysis_hints],
                "reconstruction_analysis_ignored_reasons": analysis_ignored,
                "ai_suggestions_used": ai_suggestions_used,
            },
            trace=trace,
        )

    result = provider.reconstruct_title(image, working_group, image_size)
    manual_complete_title = bool(
        working_group.get("manual")
        and working_group.get("render_mode") == "art_text"
        and result.metadata.get("title_mask_selected_candidate") in {
            "outline-solid",
            "outline-complete",
            "complete-print",
        }
    )
    mask_validation = _mask_validation(
        result.mask,
        image_size,
        result.metadata,
        max_coverage=0.24 if manual_complete_title else 0.18,
    )
    cleaned_validation = _cleaned_validation(image, result.cleaned_image, result.mask) if result.cleaned_image is not None else {"passed": False, "reason": "not_provided"}
    trace.add("mask_validation", "passed" if mask_validation["passed"] else "failed", **mask_validation)
    if result.cleaned_image is not None:
        trace.add("provider_cleaned_image_validation", "passed" if cleaned_validation["passed"] else "failed", **cleaned_validation)
    trace.add(
        "mask_candidate_summary",
        "reported",
        selected=result.metadata.get("title_mask_selected_candidate"),
        component_summary=result.metadata.get("title_mask_component_summary"),
    )
    provider_cleaned_accepted = bool(result.cleaned_image is not None and cleaned_validation["passed"])
    analysis_review_reasons = list(working_group.get("reconstruction_review_reasons", []))
    status = "accepted" if mask_validation["passed"] or provider_cleaned_accepted else "review"
    if analysis_review_reasons:
        status = "review"
        trace.add("analysis_review_gate", "review", reasons=analysis_review_reasons)
    confidence = result.confidence if status == "accepted" else 0.0
    report = TitleReconstructionReport(
        group=group.get("index"),
        provider=effective_provider,
        capabilities=result.capabilities,
        status=status,
        confidence=confidence,
        mask_quality=result.mask_quality if mask_validation["passed"] else 0.0,
        background_quality=result.background_quality,
        cleanup_quality=result.cleanup_quality if provider_cleaned_accepted else 0.0,
        cleanup_method=(
            "provider-cleaned-image"
            if provider_cleaned_accepted
            else ("provider-cleaned-image-rejected" if result.cleaned_image is not None else result.method)
        ),
        validation={"mask": mask_validation, "provider_cleaned_image": cleaned_validation},
        warning="analysis_review_required" if analysis_review_reasons else (result.warning if not mask_validation["passed"] else ""),
        metadata={
            **dict(result.metadata),
            "reconstruction_analysis_provider": analysis_provider.provider_id,
            "reconstruction_analysis_capabilities": analysis_provider.capabilities.to_dict(),
            "reconstruction_analysis_confidence": round(analysis_confidence, 3),
            "reconstruction_analysis_hints": analysis_hints,
            "reconstruction_analysis_trust_levels": [hint.get("trust_level") for hint in analysis_hints],
            "reconstruction_analysis_ignored_reasons": analysis_ignored,
            "ai_suggestions_used": ai_suggestions_used,
        },
        trace=trace,
    )
    report_dict = report.to_dict()
    if status == "accepted" and result.mask is not None:
        _RECONSTRUCTION_CACHE[key] = _CachedTitleReconstruction(result.mask.copy(), report_dict)
    result.metadata = report_dict
    return result, report


def _record_reconstruction_history(group: dict[str, Any]) -> None:
    report = group.get("title_reconstruction_report")
    if not isinstance(report, dict):
        return
    history = ReconstructionHistory.from_group(group)
    history.add_attempt(
        title_reconstruction_version=report.get("title_reconstruction_version"),
        title_reconstruction_provider=report.get("title_reconstruction_provider"),
        reconstruction_analysis_provider=report.get("reconstruction_analysis_provider"),
        title_reconstruction_status=report.get("title_reconstruction_status"),
        title_reconstruction_confidence=report.get("title_reconstruction_confidence"),
        mask_quality=report.get("mask_quality"),
        background_quality=report.get("background_quality"),
        cleanup_quality=report.get("cleanup_quality"),
        cleanup_method=report.get("cleanup_method"),
        ai_suggestions_used=list(report.get("ai_suggestions_used", [])),
        validation=report.get("validation"),
    )
    report["reconstruction_history"] = history.to_list()
    group["reconstruction_history"] = history.to_list()


def reconstruct_title_page(
    original: Image.Image,
    title_groups: list[dict[str, Any]],
    normal_mask: np.ndarray,
    *,
    output_dir: Path,
    stem: str,
    provider_id: str = "opencv",
    analysis_provider_id: str = "none",
    inpaint_python_resolver: InpaintResolver | None = None,
    configured_inpaint_python: str | None = None,
) -> PageTitleReconstruction:
    image_size = original.size
    combined_title_mask = np.zeros((image_size[1], image_size[0]), dtype=np.uint8)
    reports: list[dict[str, Any]] = []
    title_mask_reports: list[dict[str, Any]] = []
    accepted_groups: list[dict[str, Any]] = []
    provider_cleaned_images: list[Image.Image] = []

    for group in title_groups:
        result, report = reconstruct_title_group(
            original,
            group,
            image_size,
            provider_id=provider_id,
            analysis_provider_id=analysis_provider_id,
        )
        report_dict = report.to_dict()
        group["title_reconstruction_report"] = report_dict
        reports.append(report_dict)
        mask_report = {key: value for key, value in result.metadata.items() if key.startswith("title_mask_") or key == "needs_title_mask_review"}
        if mask_report:
            mask_report["group"] = group.get("index")
            group["title_mask_report"] = mask_report
            title_mask_reports.append(mask_report)
        if report.status == "accepted" and result.has_mask:
            combined_title_mask = np.maximum(combined_title_mask, result.mask)
            accepted_groups.append(group)
        elif (
            report.status == "accepted"
            and result.cleaned_image is not None
            and report.validation.get("provider_cleaned_image", {}).get("passed")
        ):
            provider_cleaned_images.append(result.cleaned_image.convert("RGB"))

    mask = np.maximum(normal_mask, combined_title_mask)
    inpaint_warning = ""
    cleaning_method = "title-glyph-mask-skipped" if title_groups else "unchanged"
    cleaned: Image.Image | None = None
    cleanup_quality = 0.0
    if provider_cleaned_images and len(provider_cleaned_images) == len(title_groups) and not np.count_nonzero(combined_title_mask):
        cleaned = provider_cleaned_images[-1]
        cleaning_method = "provider-cleaned-image"
        cleanup_quality = 0.9
        for group in title_groups:
            report = group.get("title_reconstruction_report", {})
            if isinstance(report, dict):
                report["cleanup_method"] = cleaning_method
                report["cleanup_quality"] = cleanup_quality
                report["title_reconstruction_confidence"] = round(_score(float(report.get("mask_quality", 0.0) or 0.0), cleanup_quality, 0.0), 3)
                report["reconstruction_trace"] = [
                    *list(report.get("reconstruction_trace", [])),
                    {"stage": "cleanup", "status": "passed", "method": cleaning_method},
                ]
    elif np.count_nonzero(combined_title_mask):
        try:
            executable = configured_inpaint_python or (inpaint_python_resolver() if inpaint_python_resolver else None)
            cleaned, cleaning_method = clean_art_text_background(
                original,
                Image.fromarray(mask),
                output_dir,
                stem,
                python_executable=executable,
            )
            validation = _cleaned_validation(original, cleaned, mask)
            cleanup_quality = 0.9 if validation["passed"] else 0.0
        except InpaintRuntimeUnavailable as error:
            cleaned = None
            validation = {"passed": False, "reason": "inpaint_runtime_unavailable"}
            inpaint_warning = str(error)
            cleaning_method = "opencv-inpaint-fallback:inpaint_runtime_unavailable"
        except Exception as error:
            cleaned = None
            validation = {"passed": False, "reason": type(error).__name__}
            inpaint_warning = str(error) or type(error).__name__
            cleaning_method = f"opencv-inpaint-fallback:{type(error).__name__}"
        if cleaned is None or not validation["passed"]:
            source_array = np.asarray(original)
            cleaned_array, cleaning_method = clean_background(source_array, mask)
            cleaned = Image.fromarray(cleaned_array)
            validation = _cleaned_validation(original, cleaned, mask)
            cleanup_quality = 0.72 if validation["passed"] else 0.0
            del source_array, cleaned_array
        for group in accepted_groups:
            report = group.get("title_reconstruction_report", {})
            if isinstance(report, dict):
                report["cleanup_method"] = cleaning_method
                report["cleanup_quality"] = round(cleanup_quality, 3)
                report["validation"] = {**dict(report.get("validation", {})), "cleanup": validation}
                report["title_reconstruction_confidence"] = round(_score(float(report.get("mask_quality", 0.0) or 0.0), cleanup_quality, 0.0), 3)
                report["reconstruction_trace"] = [
                    *list(report.get("reconstruction_trace", [])),
                    {"stage": "cleanup", "status": "passed" if validation["passed"] else "failed", "method": cleaning_method, "validation": validation},
                ]
    elif cleaned is None:
        source_array = np.asarray(original)
        cleaned_array, base_method = clean_background(source_array, normal_mask)
        cleaned = Image.fromarray(cleaned_array)
        del source_array, cleaned_array
        if np.count_nonzero(normal_mask):
            cleaning_method = base_method
        inpaint_warning = "No safe title glyph mask was accepted; title cleanup was skipped."

    background_plate_reports: list[dict[str, Any]] = []
    if title_groups and not np.count_nonzero(combined_title_mask):
        plate_result = apply_title_background_plates(cleaned, title_groups, image_size)
        background_plate_reports = plate_result.reports
        if plate_result.accepted:
            cleaned = plate_result.image
            cleaning_method = "background-plate-fallback"
            inpaint_warning = ""
        else:
            inpaint_warning = "No safe title glyph mask or background plate was accepted; title cleanup was skipped."
        for group in title_groups:
            plate_report = group.get("title_background_plate_report")
            reconstruction_report = group.get("title_reconstruction_report")
            if not isinstance(reconstruction_report, dict):
                continue
            accepted_plate = isinstance(plate_report, dict) and not plate_report.get("title_background_plate_warning")
            reconstruction_report["title_reconstruction_status"] = "accepted" if accepted_plate else "review"
            reconstruction_report["background_quality"] = float(plate_report.get("title_background_plate_confidence", 0.0) if isinstance(plate_report, dict) else 0.0)
            reconstruction_report["cleanup_quality"] = reconstruction_report["background_quality"]
            reconstruction_report["cleanup_method"] = "background-plate-fallback" if accepted_plate else "title-glyph-mask-skipped"
            reconstruction_report["title_reconstruction_confidence"] = reconstruction_report["background_quality"]
            reconstruction_report["validation"] = {**dict(reconstruction_report.get("validation", {})), "background": plate_report or {}}
            reconstruction_report["reconstruction_trace"] = [
                *list(reconstruction_report.get("reconstruction_trace", [])),
                {"stage": "background_plate", "status": "passed" if accepted_plate else "failed", "report": plate_report or {}},
                {"stage": "result", "status": reconstruction_report["title_reconstruction_status"]},
            ]
    else:
        for group in title_groups:
            reconstruction_report = group.get("title_reconstruction_report")
            if isinstance(reconstruction_report, dict):
                reconstruction_report["reconstruction_trace"] = [
                    *list(reconstruction_report.get("reconstruction_trace", [])),
                    {"stage": "result", "status": reconstruction_report.get("title_reconstruction_status", "accepted")},
                ]

    for group in title_groups:
        _record_reconstruction_history(group)

    reports = [dict(group.get("title_reconstruction_report", report)) for group, report in zip(title_groups, reports)]
    accepted_count = sum(1 for report in reports if report.get("title_reconstruction_status") == "accepted")
    needs_review = bool(title_groups and accepted_count < len(title_groups))
    return PageTitleReconstruction(
        image=cleaned,
        mask=mask,
        cleaning_method=cleaning_method,
        reports=reports,
        title_mask_reports=title_mask_reports,
        background_plate_reports=background_plate_reports,
        accepted_count=accepted_count,
        needs_review=needs_review,
        inpaint_warning=inpaint_warning,
    )
