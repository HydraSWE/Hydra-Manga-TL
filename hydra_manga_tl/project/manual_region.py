"""Asynchronous OCR and translation for user-drawn text rectangles."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, Signal, Slot

from hydra_manga_tl.phase.layout import compose_manual_region, TextGroup
from hydra_manga_tl.ocr.core import OCRResult
from hydra_manga_tl.ocr.service import OCRService, regions_with_review_metadata
from hydra_manga_tl.translation.engines import PageDialogue
from hydra_manga_tl.translation.queue import (
    CancellationToken,
    RequestCancelled,
    TRANSLATION_QUEUE,
    TranslationQueue,
)
from hydra_manga_tl.translation.requests import (
    TranslationRequest,
    TranslationRequestStatus,
)
from hydra_manga_tl.translation.cache_store import TRANSLATION_CACHE
from hydra_manga_tl.core.normalization import normalize_global_text
from hydra_manga_tl.core.user_errors import manual_translation_error
from hydra_manga_tl.title.models import TitleComposition
from hydra_manga_tl.translation.runtime import TRANSLATION_RUNTIME
from hydra_manga_tl.translation.memory import source_region_hash, source_text_hash

LOGGER = logging.getLogger(__name__)


def manual_region_user_message(error: BaseException | str) -> str:
    """Return a concise dialog-safe message while logs keep full details."""
    return manual_translation_error(error)


def normalize_image_rect(start, end, image_size: tuple[int, int], minimum: int = 8) -> list[int] | None:
    """Normalize, clamp, and validate a rectangle in source-image coordinates."""
    width, height = image_size
    x1, x2 = sorted((round(float(start[0])), round(float(end[0]))))
    y1, y2 = sorted((round(float(start[1])), round(float(end[1]))))
    rect = [max(0, x1), max(0, y1), min(width, x2), min(height, y2)]
    if rect[2] - rect[0] < minimum or rect[3] - rect[1] < minimum:
        return None
    return rect


def rect_to_polygon(rect: list[int] | tuple[int, int, int, int]) -> list[list[int]]:
    """Return a four-point polygon for a rectangular region."""
    x1, y1, x2, y2 = [int(value) for value in rect]
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def polygon_bounding_rect(polygon: list[list[int]] | tuple[tuple[int, int], ...]) -> list[int] | None:
    """Return the bounding rectangle for a polygon, or None for invalid input."""
    if len(polygon) < 3:
        return None
    xs = [int(point[0]) for point in polygon]
    ys = [int(point[1]) for point in polygon]
    rect = [min(xs), min(ys), max(xs), max(ys)]
    if rect[2] <= rect[0] or rect[3] <= rect[1]:
        return None
    return rect


def normalize_image_polygon(
    points: list[list[float]] | tuple[tuple[float, float], ...],
    image_size: tuple[int, int],
    *,
    minimum: int = 8,
) -> list[list[int]] | None:
    """Clamp and validate a polygon in source-image coordinates."""
    if len(points) < 3:
        return None
    width, height = image_size
    polygon: list[list[int]] = []
    for point in points:
        x = max(0, min(width, round(float(point[0]))))
        y = max(0, min(height, round(float(point[1]))))
        if not polygon or polygon[-1] != [x, y]:
            polygon.append([x, y])
    if len(polygon) > 1 and polygon[0] == polygon[-1]:
        polygon.pop()
    rect = polygon_bounding_rect(polygon)
    if rect is None or rect[2] - rect[0] < minimum or rect[3] - rect[1] < minimum:
        return None
    if polygon_area(polygon) <= 0:
        return None
    if polygon_self_intersects(polygon):
        return None
    return polygon


def normalize_geometry_polygon(value: object) -> list[list[int]]:
    polygon: list[list[int]] = []
    if not isinstance(value, (list, tuple)):
        return polygon
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            polygon.append([int(point[0]), int(point[1])])
        except (TypeError, ValueError):
            continue
    return polygon


def normalize_geometry_polygons(value: object) -> list[list[list[int]]]:
    polygons: list[list[list[int]]] = []
    if not isinstance(value, (list, tuple)):
        return polygons
    for polygon in value:
        normalized = normalize_geometry_polygon(polygon)
        if normalized:
            polygons.append(normalized)
    return polygons


def cleanup_polygons_from_ocr_regions(source_regions: list[dict]) -> list[list[list[int]]]:
    """Return OCR-region polygons for cleanup without reusing the user selection."""
    return normalize_geometry_polygons([
        region.get("polygon", [])
        for region in source_regions
        if isinstance(region, dict)
    ])


def cleanup_polygons_for_manual_region(
    source_regions: list[dict],
    composed: TextGroup,
    selection_polygon: list[list[int]],
) -> list[list[list[int]]]:
    cleanup = cleanup_polygons_from_ocr_regions(source_regions)
    if cleanup:
        return cleanup
    if composed.bbox and len(composed.bbox) == 4:
        return [rect_to_polygon(composed.bbox)]
    return [selection_polygon]


def _compact_mark_text(text: object) -> str:
    return re.sub(r"\s+", "", str(text or "").strip())


def _is_decorative_mark_char(char: str) -> bool:
    codepoint = ord(char)
    if char in "♪♫♬♩♡♥❤★☆◇◆■□●○◎※〆〒":
        return True
    if unicodedata.category(char).startswith("S"):
        return True
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or 0xFE00 <= codepoint <= 0xFE0F
    )


def _is_preservable_mark_text(text: object) -> bool:
    compact = _compact_mark_text(text)
    if not compact:
        return False
    stripped = compact.strip(".,!?！？。…・ッっー~〜-")
    if not stripped:
        return False
    return all(_is_decorative_mark_char(char) for char in stripped)


def _region_bbox(polygon: list[list[int]]) -> list[int]:
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def decorative_symbols_from_ocr_regions(source_regions: list[dict]) -> list[dict]:
    symbols: list[dict] = []
    for index, region in enumerate(source_regions, 1):
        if not isinstance(region, dict) or not _is_preservable_mark_text(region.get("text", "")):
            continue
        polygon = normalize_geometry_polygon(region.get("polygon", []))
        if not polygon:
            continue
        symbols.append({
            "id": f"symbol:{index}",
            "kind": "decorative_symbol",
            "text": str(region.get("text", "")),
            "confidence": float(region.get("confidence", 0.0) or 0.0),
            "polygon": polygon,
            "bbox": _region_bbox(polygon),
            "render_policy": "redraw_semantic",
            "source": "ocr",
        })
    return symbols


def preserved_marks_from_ocr_regions(source_regions: list[dict]) -> list[dict]:
    return []


def split_manual_source_regions(source_regions: list[dict]) -> tuple[list[dict], list[dict]]:
    text_regions: list[dict] = []
    mark_regions: list[dict] = []
    for region in source_regions:
        if isinstance(region, dict) and _is_preservable_mark_text(region.get("text", "")):
            mark_regions.append(region)
        else:
            text_regions.append(region)
    if not text_regions:
        return source_regions, []
    return text_regions, decorative_symbols_from_ocr_regions(mark_regions)


def polygon_area(polygon: list[list[int]]) -> float:
    area = 0.0
    for index, point in enumerate(polygon):
        next_point = polygon[(index + 1) % len(polygon)]
        area += point[0] * next_point[1] - next_point[0] * point[1]
    return abs(area) / 2.0


def polygon_self_intersects(polygon: list[list[int]]) -> bool:
    """Return True when non-adjacent polygon edges cross."""
    if len(polygon) < 4:
        return False

    def orientation(a: list[int], b: list[int], c: list[int]) -> int:
        value = (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])
        if value == 0:
            return 0
        return 1 if value > 0 else 2

    def on_segment(a: list[int], b: list[int], c: list[int]) -> bool:
        return (
            min(a[0], c[0]) <= b[0] <= max(a[0], c[0])
            and min(a[1], c[1]) <= b[1] <= max(a[1], c[1])
        )

    def intersects(a: list[int], b: list[int], c: list[int], d: list[int]) -> bool:
        o1, o2 = orientation(a, b, c), orientation(a, b, d)
        o3, o4 = orientation(c, d, a), orientation(c, d, b)
        if o1 != o2 and o3 != o4:
            return True
        return (
            (o1 == 0 and on_segment(a, c, b))
            or (o2 == 0 and on_segment(a, d, b))
            or (o3 == 0 and on_segment(c, a, d))
            or (o4 == 0 and on_segment(c, b, d))
        )

    edges = [(polygon[index], polygon[(index + 1) % len(polygon)]) for index in range(len(polygon))]
    last_index = len(edges) - 1
    for first, (a, b) in enumerate(edges):
        for second, (c, d) in enumerate(edges[first + 1 :], start=first + 1):
            if second == first + 1 or (first == 0 and second == last_index):
                continue
            if intersects(a, b, c, d):
                return True
    return False


def overlapping_auto_indices(groups: list[dict], rect: list[int]) -> list[int]:
    """Return auto groups substantially covered by a manual rectangle."""
    mx1, my1, mx2, my2 = rect
    overlaps: list[int] = []
    for group in groups:
        polygon = group.get("polygon", [])
        if not polygon or group.get("manual"):
            continue
        xs, ys = [point[0] for point in polygon], [point[1] for point in polygon]
        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        center_inside = mx1 <= (x1 + x2) / 2 <= mx2 and my1 <= (y1 + y2) / 2 <= my2
        intersection = max(0, min(mx2, x2) - max(mx1, x1)) * max(0, min(my2, y2) - max(my1, y1))
        coverage = intersection / max(1, (x2 - x1) * (y2 - y1))
        if center_inside or coverage >= 0.50:
            overlaps.append(int(group["index"]))
    return overlaps


def _manual_ocr_languages(source_language: str) -> tuple[str, ...]:
    mapping = {"Japanese": "japan", "Chinese": "ch", "Korean": "ko", "Latin-script": "en"}
    preferred = mapping.get(source_language)
    return (preferred,) if preferred else ("japan",)


def _selection_cache_key(source: Path, rect: list[int], preferred: str, quality: str) -> str:
    return TRANSLATION_CACHE.manual_selection_key(source, rect, preferred, quality)


def _read_cached_selection(cache_dir: Path, key: str) -> OCRResult | None:
    path = cache_dir / f"manual_{key[:16]}.json"
    if not path.is_file():
        return None
    try:
        payload = TRANSLATION_CACHE.read_json(path)
        if payload is None:
            return None
        metadata = payload.get("metadata", {})
        if metadata.get("selection_variant") != "color2_padded_v2":
            LOGGER.info("Manual box: invalidating legacy cache payload variant=%s", metadata.get("selection_variant"))
            return None
        return OCRResult.from_dict(payload)
    except (ValueError, TypeError):
        return None


def _write_cached_selection(cache_dir: Path, key: str, result: OCRResult) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"manual_{key[:16]}.json"
    result.metadata = {**result.metadata, "manual_cache_path": str(path)}
    TRANSLATION_CACHE.write_json(path, result.to_dict())


def _manual_timing_path(request: dict) -> Path | None:
    cache_path = request.get("cache_path")
    if not cache_path:
        return None
    request_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(request.get("request_id") or "manual"))
    return Path(cache_path).parent / "manual_timings" / f"{request_id}.json"


def _write_manual_timing(request: dict, timing: dict) -> None:
    path = _manual_timing_path(request)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        TRANSLATION_CACHE.write_json(path, timing)
    except Exception:
        LOGGER.exception("Manual box: failed to write timing report")


def _ordered_title_members(
    source_regions: list[dict],
    member_indices: list[int],
    fallback_polygon: list[list[int]],
) -> tuple[list[dict], list[list[list[int]]]]:
    members = [
        source_regions[index - 1]
        for index in member_indices
        if 0 < index <= len(source_regions)
        and str(source_regions[index - 1].get("text", "")).strip()
    ]
    polygons: list[list[list[int]]] = []
    for region in members:
        raw_polygon = region.get("polygon")
        try:
            polygon = [[int(point[0]), int(point[1])] for point in raw_polygon]
        except (TypeError, ValueError):
            polygon = []
        polygons.append(polygon if len(polygon) >= 3 else [list(point) for point in fallback_polygon])
    return members, polygons


def _generic_title_composition(
    request_id: str,
    members: list[dict],
    polygons: list[list[list[int]]],
    translations: list[str],
    style_profile: dict | None,
) -> dict:
    if not members or not polygons or not translations:
        return {}
    group = {
        "index": request_id or "manual-title",
        "translated_text": " ".join(translations),
        "source_member_texts": [str(member.get("text", "")) for member in members],
        "title_layer_translations": translations,
        "style_profile": dict(style_profile) if isinstance(style_profile, dict) else {},
        "render_mode": "art_text",
        "bubble_type": "title",
    }
    return TitleComposition.from_group(group, translations, polygons).to_dict()


class _ManualRegionProcessor(QObject):
    stage = Signal(str, str)
    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._ocr = None
        self.checkpoint = None

    def _stage(self, status: TranslationRequestStatus, message: str) -> None:
        LOGGER.info("Manual box: %s", message)
        if self.checkpoint is not None:
            self.checkpoint(status, message)
        self.stage.emit(status.value, message)

    @Slot(object)
    def process(self, request: dict) -> None:
        started = time.perf_counter()
        timing: dict = {
            "request_id": str(request.get("request_id") or ""),
            "project_id": str(request.get("project_id") or ""),
            "image_id": str(request.get("image_id") or ""),
            "image_index": int(request.get("image_index", -1)),
            "translation_engine": str(request.get("translation_engine") or ""),
            "queued_at_utc": request.get("queued_at_utc"),
            "queue_delay_seconds": None,
            "stages": [],
            "status": "running",
        }
        queued_at_monotonic = request.get("queued_at_monotonic")
        try:
            if queued_at_monotonic is not None:
                timing["queue_delay_seconds"] = max(
                    0.0,
                    started - float(queued_at_monotonic),
                )
        except (TypeError, ValueError):
            timing["queue_delay_seconds"] = None

        def record_stage(name: str, stage_started: float, **extra) -> None:
            elapsed = time.perf_counter() - stage_started
            entry = {"name": name, "seconds": round(elapsed, 3)}
            entry.update(extra)
            timing["stages"].append(entry)
            LOGGER.info("Manual box: timing %s=%.3fs", name, elapsed)

        try:
            self._stage(TranslationRequestStatus.OCR, "Reading selected text")
            rect = [int(value) for value in request["rect"]]
            polygon = normalize_image_polygon(
                request.get("polygon") or rect_to_polygon(rect),
                (rect[2], rect[3]),
                minimum=1,
            ) or rect_to_polygon(rect)
            LOGGER.info(
                "Manual box: selected rect=%s polygon_points=%d image=%s",
                rect,
                len(polygon),
                request.get("source_path", ""),
            )
            source_language = request.get("source_language", "")
            preferred = _manual_ocr_languages(source_language)[0]
            source_path = Path(request["source_path"])
            quality = request.get("quality", "Balanced")
            cache_dir = Path(request.get("ocr_cache_dir") or Path(request["cache_path"]).parent / "ocr_cache")
            cache_key = _selection_cache_key(source_path, rect, preferred, quality)
            cache_started = time.perf_counter()
            force_refresh = bool(request.get("bypass_cache")) or bool(request.get("force_refresh"))
            cached = None if force_refresh else _read_cached_selection(cache_dir, cache_key)
            record_stage("ocr_cache_lookup", cache_started, cache_hit=cached is not None)
            if cached is not None:
                LOGGER.info("Manual box: OCR cache hit language=%s quality=%s", preferred, quality)
                ocr_result = cached
                source_regions = regions_with_review_metadata(ocr_result)
            else:
                LOGGER.info("Manual box: OCR cache miss, analyzing selection language=%s quality=%s", preferred, quality)
                if self._ocr is None:
                    service_started = time.perf_counter()
                    self._ocr = OCRService(
                        (preferred,),
                        use_subprocess=bool(request.get("ocr_subprocess_enabled", False)),
                        recycle_pages=int(request.get("ocr_worker_recycle_pages", 25)),
                        memory_limit_mb=int(request.get("ocr_worker_memory_limit_mb", 2048)),
                    )
                    record_stage("ocr_service_init", service_started)
                elif force_refresh and self._ocr.worker is not None:
                    self._ocr.worker.restart()
                ocr_started = time.perf_counter()
                service_result = self._ocr.analyze_selection(
                    source_path, rect, preferred_language=preferred, quality=quality,
                )
                record_stage("ocr_analyze_selection", ocr_started)
                ocr_result = service_result.ocr_result
                cache_write_started = time.perf_counter()
                _write_cached_selection(cache_dir, cache_key, ocr_result)
                record_stage("ocr_cache_write", cache_write_started)
                source_regions = service_result.final_regions
            LOGGER.info("Manual box: OCR produced %d region(s)", len(source_regions))
            source_language = source_language or ocr_result.language
            bubble_type = str(request.get("bubble_type") or "dialogue")
            detected_regions = list(source_regions)
            decorative_symbols: list[dict] = []
            if bubble_type != "title":
                source_regions, decorative_symbols = split_manual_source_regions(source_regions)
                if decorative_symbols:
                    LOGGER.info(
                        "Manual box: detected %d redrawable decorative OCR symbol(s)",
                        len(decorative_symbols),
                    )
            composed = compose_manual_region(source_regions)
            title_members: list[dict] = []
            title_polygons: list[list[list[int]]] = []
            if bubble_type == "title":
                title_members, title_polygons = _ordered_title_members(
                    source_regions,
                    composed.member_indices,
                    polygon,
                )
            if source_language == "Latin-script" and request["target"] == "en":
                raise ValueError("The selected text already appears to be English.")
            confidence = min(source_regions[index - 1]["confidence"] for index in composed.member_indices)
            translated_text = ""
            review_reasons: list[str] = list(dict.fromkeys(
                f"ocr:{reason}"
                for region in source_regions
                for reason in region.get("ocr_review_reasons", [])
                if str(reason)
            ))
            if (
                source_language == "Japanese"
                and sum(char.isascii() and char.isdigit() for char in composed.text) >= 3
            ):
                review_reasons.append("ocr:suspicious_digits")

            if composed.direction == "vertical-rtl" and len(composed.member_indices) > 1:
                review_reasons.append("ocr:vertical_order_suspect")

            status = "translated"
            self._stage(TranslationRequestStatus.TRANSLATING, "Translating selected text")
            LOGGER.info(
                "Manual box: translating %d character(s) with engine=%s",
                len(composed.text),
                request.get("translation_engine", ""),
            )

            page_context = (
                "These ordered blocks are visual segments of one manga title. Translate every block with the same ID so the combined English reads as one concise title. Each returned block will inherit the corresponding source segment's color and geometry."
                if bubble_type == "title"
                else (
                    "Manual user-selected text box. Translate only the selected manga text. "
                    "Any decorative_symbols metadata describes hearts, music notes, stars, or similar bubble symbols. "
                    "Do not translate those symbols as words; reproduce them naturally as symbols in the returned text when they carry tone or emotion."
                )
            )
            dialogue = (
                [
                    {
                        "id": f"title:{index}",
                        "text": str(member.get("text", "")),
                        "confidence": float(member.get("confidence", confidence)),
                        "bbox": title_polygons[index - 1],
                        "source_direction": composed.direction,
                        "ocr_review_reasons": review_reasons,
                        "source_member_texts": [str(member.get("text", ""))],
                    }
                    for index, member in enumerate(title_members, 1)
                ]
                if title_members
                else [{
                    "id": "manual:1",
                    "text": composed.text,
                    "confidence": confidence,
                    "bbox": polygon,
                    "source_direction": composed.direction,
                    "ocr_review_reasons": list(dict.fromkeys(
                        reason for region in source_regions for reason in region.get("ocr_review_reasons", []) if reason
                    )),
                    "source_member_texts": [str(region.get("text", "")) for region in source_regions],
                    "decorative_symbols": decorative_symbols,
                    "preserved_marks": [],
                }]
            )
            page = PageDialogue(
                source_language=source_language,
                target_language=request["target"],
                dialogue=dialogue,
                page_context=page_context,
            )
            translation_started = time.perf_counter()
            provider = str(request.get("translation_engine", "") or "").strip().lower()
            if provider == "openai_compatible":
                provider_models = dict(request.get("provider_models", {}) or {})
                provider_base_urls = dict(request.get("provider_base_urls", {}) or {})
                LOGGER.info(
                    "Manual box: OpenAI-compatible translation started units=%d base_url=%s model=%s",
                    len(dialogue),
                    provider_base_urls.get("openai_compatible", ""),
                    provider_models.get("openai_compatible", ""),
                )
            try:
                page_result = TRANSLATION_RUNTIME.translate_page(page, request)
            except Exception as error:
                if provider == "openai_compatible":
                    LOGGER.warning(
                        "Manual box: OpenAI-compatible translation failed units=%d error=%s",
                        len(dialogue),
                        error,
                    )
                raise
            if provider == "openai_compatible":
                LOGGER.info(
                    "Manual box: OpenAI-compatible translation finished units=%d returned=%d provider=%s",
                    len(dialogue),
                    len(page_result.translations),
                    TRANSLATION_RUNTIME.last_engine_id,
                )
            record_stage("translate_page", translation_started, provider=TRANSLATION_RUNTIME.last_engine_id)
            translated_by_id = {
                str(item.get("id", "")): normalize_global_text(str(item.get("text", "")))
                for item in page_result.translations
            }
            result_by_id = {
                str(item.get("id", "")): dict(item)
                for item in page_result.translations
            }
            memory_units = [
                {
                    "id": str(item.get("id", "")),
                    "source_text": str(item.get("text", "")),
                    "translated_text": translated_by_id.get(
                        str(item.get("id", "")),
                        "",
                    ),
                    "region_type": bubble_type,
                    "source_text_hash": item.get("source_text_hash", ""),
                    "source_region_hash": item.get("source_region_hash"),
                    "translation_source": result_by_id.get(
                        str(item.get("id", "")),
                        {},
                    ).get("translation_source", "provider"),
                    "provider_id": result_by_id.get(
                        str(item.get("id", "")),
                        {},
                    ).get("provider_id", TRANSLATION_RUNTIME.last_engine_id),
                    "tm_entry_id": result_by_id.get(
                        str(item.get("id", "")),
                        {},
                    ).get("tm_entry_id"),
                }
                for item in dialogue
            ]
            translated_segments = [
                translated_by_id.get(str(item["id"]), "")
                for item in dialogue
            ]
            if any(not segment for segment in translated_segments):
                raise ValueError("The selected translation engine did not return every selected text segment.")
            translated_text = normalize_global_text(" ".join(translated_segments))
            if not translated_text:
                raise ValueError("The selected translation engine did not return usable text.")
            if not translated_text or "translation_unchanged" in review_reasons:
                raise ValueError("The selected text could not be translated reliably.")
            review_reasons = list(dict.fromkeys(review_reasons))
            if review_reasons:
                status = "review"
            LOGGER.info(
                "Manual box: translated status=%s confidence=%.3f text_chars=%d",
                status,
                confidence,
                len(translated_text),
            )
            explicit_composition = dict(request.get("title_composition") or {})
            title_composition = (
                explicit_composition
                if explicit_composition or bubble_type != "title"
                else _generic_title_composition(
                    str(request.get("request_id") or ""),
                    title_members,
                    title_polygons,
                    translated_segments,
                    request.get("style_profile"),
                )
            )
            selection_polygon = polygon
            placement_polygon = polygon
            cleanup_polygons = (
                title_polygons
                if title_polygons
                else cleanup_polygons_for_manual_region(
                    detected_regions,
                    composed,
                    selection_polygon,
                )
            )
            self.succeeded.emit({
                "request_id": str(request.get("request_id") or ""),
                "project_id": request["project_id"], "image_id": request["image_id"],
                "image_index": request["image_index"], "id": str(uuid4()), "rect": rect,
                "polygon": selection_polygon,
                "selection_polygon": selection_polygon,
                "cleanup_polygons": cleanup_polygons,
                "placement_polygon": placement_polygon,
                "decorative_symbols": decorative_symbols,
                "preserved_marks": [],
                "source_polygons": title_polygons if title_polygons else [selection_polygon],
                "original_text": composed.text,
                "source_member_texts": (
                    [str(member.get("text", "")) for member in title_members]
                    if title_members
                    else [str(region.get("text", "")) for region in source_regions]
                ),
                "translated_text": translated_text, "ocr_confidence": confidence,
                "source_language": source_language, "direction": composed.direction,
                "status": status, "review_reasons": review_reasons,
                "suppressed_auto_group_indices": [],
                "bubble_type": bubble_type,
                "render_mode": str(request.get("render_mode") or ""),
                "title_composition": title_composition,
                "title_reconstruction": dict(request.get("title_reconstruction") or {}),
                "style_profile": request.get("style_profile"),
                "source_text_hash": source_text_hash(composed.text),
                "source_region_hash": source_region_hash(
                    source_path,
                    title_polygons if title_polygons else [selection_polygon],
                ),
                "translation_source": (
                    "translation-memory"
                    if memory_units
                    and all(
                        unit.get("translation_source") == "translation-memory"
                        for unit in memory_units
                    )
                    else "provider"
                ),
                "translation_provider": str(
                    memory_units[0].get("provider_id", "")
                    if memory_units
                    else TRANSLATION_RUNTIME.last_engine_id
                ),
                "translation_memory_units": memory_units,
            })
            timing["status"] = "succeeded"
        except RequestCancelled:
            timing["status"] = "cancelled"
            raise
        except Exception as error:
            timing["status"] = "failed"
            timing["error"] = f"{type(error).__name__}: {error}"
            LOGGER.exception("Manual box: failed while processing selection")
            self.failed.emit({
                "project_id": request.get("project_id", ""), "image_id": request.get("image_id", ""),
                "image_index": request.get("image_index", -1), "message": manual_region_user_message(error),
            })
        finally:
            timing["total_seconds"] = round(time.perf_counter() - started, 3)
            _write_manual_timing(request, timing)
            LOGGER.info(
                "Manual box: finished status=%s total=%.3fs timing_path=%s",
                timing.get("status"),
                timing["total_seconds"],
                _manual_timing_path(request) or "",
            )


class ManualRegionService(QObject):
    """Compatibility facade that submits manual requests to the shared queue."""

    succeeded = Signal(object)
    failed = Signal(object)
    busy_changed = Signal(bool)
    state_changed = Signal(str, str, str)

    def __init__(self, queue: TranslationQueue | None = None) -> None:
        super().__init__()
        self._queue = queue or TRANSLATION_QUEUE
        self._active: dict[str, TranslationRequest] = {}
        self._queue.state_changed.connect(self._on_queue_state_changed)
        self._queue.completed.connect(self._on_queue_completed)
        self._queue.failed.connect(self._on_queue_failed)

    @property
    def busy(self) -> bool:
        return bool(self._active)

    @property
    def active_request_ids(self) -> tuple[str, ...]:
        return tuple(self._active)

    def submit(self, request: dict | TranslationRequest) -> bool:
        if self.busy:
            LOGGER.info("Manual box: request ignored because another manual box is active")
            return False
        typed_request = (
            request if isinstance(request, TranslationRequest)
            else TranslationRequest.from_legacy_manual(request)
        )
        LOGGER.info(
            "Manual box: queued request_id=%s image_index=%s",
            typed_request.request_id,
            typed_request.image_index,
        )
        typed_request.metadata["queued_at_monotonic"] = time.perf_counter()
        typed_request.metadata["queued_at_utc"] = typed_request.created_at.isoformat()
        self._active[typed_request.request_id] = typed_request
        self.busy_changed.emit(True)
        try:
            self._queue.submit(typed_request, self._handle_request)
        except Exception:
            self._active.pop(typed_request.request_id, None)
            self.busy_changed.emit(False)
            raise
        return True

    @staticmethod
    def _handle_request(
        request: TranslationRequest,
        token: CancellationToken,
        progress,
    ) -> dict:
        processor = _ManualRegionProcessor()
        outcome: dict[str, dict] = {}

        processor.checkpoint = lambda status, message: progress(status, message)
        processor.succeeded.connect(
            lambda result: outcome.__setitem__("result", result),
            Qt.ConnectionType.DirectConnection,
        )
        processor.failed.connect(
            lambda result: outcome.__setitem__("error", result),
            Qt.ConnectionType.DirectConnection,
        )
        payload = dict(request.metadata)
        payload["request_id"] = request.request_id
        token.raise_if_cancelled()
        processor.process(payload)
        token.raise_if_cancelled()
        if "error" in outcome:
            raise ManualRequestError(outcome["error"])
        if "result" not in outcome:
            raise RuntimeError("Manual request ended without a result")
        return outcome["result"]

    @Slot(str, str, str)
    def _on_queue_state_changed(self, request_id: str, status: str, message: str) -> None:
        if request_id in self._active and status != TranslationRequestStatus.DONE.value:
            self.state_changed.emit(request_id, status, message)
        if status == TranslationRequestStatus.CANCELLED.value:
            if self._active.pop(request_id, None) is not None:
                LOGGER.info("Manual box: queue cancelled request_id=%s", request_id)
                self.busy_changed.emit(bool(self._active))

    @Slot(str, object)
    def _on_queue_completed(self, request_id: str, result: dict) -> None:
        if self._active.pop(request_id, None) is None:
            return
        LOGGER.info("Manual box: queue completed request_id=%s", request_id)
        self.busy_changed.emit(bool(self._active))
        self.succeeded.emit(result)

    @Slot(str, object)
    def _on_queue_failed(self, request_id: str, result: dict) -> None:
        request = self._active.pop(request_id, None)
        if request is None:
            return
        LOGGER.info("Manual box: queue failed request_id=%s", request_id)
        self.busy_changed.emit(bool(self._active))
        error_payload = dict(result)
        details = error_payload.pop("manual_result", None)
        if isinstance(details, dict):
            error_payload.update(details)
        error_payload.update({
            "project_id": request.project_id,
            "image_id": request.image_id,
            "image_index": request.image_index,
        })
        self.failed.emit(error_payload)

    def cancel(self, request_id: str) -> bool:
        return self._queue.cancel(request_id)

    def shutdown(self) -> None:
        self.cancel_all()

    def cancel_all(self) -> bool:
        cancelled = False
        for request_id in tuple(self._active):
            cancelled = self._queue.cancel(request_id) or cancelled
        return cancelled


class ManualRequestError(RuntimeError):
    def __init__(self, result: dict) -> None:
        self.result = dict(result)
        super().__init__(str(result.get("message") or "Manual translation failed"))
