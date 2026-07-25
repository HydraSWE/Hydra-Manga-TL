"""Asynchronous OCR and translation for user-drawn text rectangles."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, Signal, Slot

from .layout import compose_manual_region
from .ocr import OCRResult
from .ocr_service import OCRService, regions_with_review_metadata
from .translation_engines import PageDialogue
from .translation_queue import (
    CancellationToken,
    TRANSLATION_QUEUE,
    TranslationQueue,
)
from .translation_requests import (
    TranslationRequest,
    TranslationRequestStatus,
)
from .translation_cache_store import TRANSLATION_CACHE
from .translation_runtime import TRANSLATION_RUNTIME


def normalize_image_rect(start, end, image_size: tuple[int, int], minimum: int = 8) -> list[int] | None:
    """Normalize, clamp, and validate a rectangle in source-image coordinates."""
    width, height = image_size
    x1, x2 = sorted((round(float(start[0])), round(float(end[0]))))
    y1, y2 = sorted((round(float(start[1])), round(float(end[1]))))
    rect = [max(0, x1), max(0, y1), min(width, x2), min(height, y2)]
    if rect[2] - rect[0] < minimum or rect[3] - rect[1] < minimum:
        return None
    return rect


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
    preferred = {"Japanese": "japan", "Chinese": "ch", "Latin-script": "en"}.get(source_language)
    return (preferred,) if preferred else ("japan",)


def _selection_cache_key(source: Path, rect: list[int], preferred: str, quality: str) -> str:
    return TRANSLATION_CACHE.manual_selection_key(source, rect, preferred, quality)


def _read_cached_selection(cache_dir: Path, key: str) -> OCRResult | None:
    path = cache_dir / f"manual_{key[:16]}.json"
    if not path.is_file():
        return None
    try:
        payload = TRANSLATION_CACHE.read_json(path)
        return OCRResult.from_dict(payload) if payload is not None else None
    except (ValueError, TypeError):
        return None


def _write_cached_selection(cache_dir: Path, key: str, result: OCRResult) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"manual_{key[:16]}.json"
    result.metadata = {**result.metadata, "manual_cache_path": str(path)}
    TRANSLATION_CACHE.write_json(path, result.to_dict())


class _ManualRegionProcessor(QObject):
    stage = Signal(str, str)
    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._ocr = None
        self.checkpoint = None

    def _stage(self, status: TranslationRequestStatus, message: str) -> None:
        if self.checkpoint is not None:
            self.checkpoint(status, message)
        self.stage.emit(status.value, message)

    @Slot(object)
    def process(self, request: dict) -> None:
        try:
            self._stage(TranslationRequestStatus.OCR, "Reading selected text")
            rect = [int(value) for value in request["rect"]]
            source_language = request.get("source_language", "")
            preferred = _manual_ocr_languages(source_language)[0]
            source_path = Path(request["source_path"])
            quality = request.get("quality", "Balanced")
            cache_dir = Path(request.get("ocr_cache_dir") or Path(request["cache_path"]).parent / "ocr_cache")
            cache_key = _selection_cache_key(source_path, rect, preferred, quality)
            cached = _read_cached_selection(cache_dir, cache_key)
            if cached is not None:
                ocr_result = cached
                source_regions = regions_with_review_metadata(ocr_result)
            else:
                if self._ocr is None:
                    self._ocr = OCRService(
                        (preferred,),
                        use_subprocess=bool(request.get("ocr_subprocess_enabled", False)),
                        recycle_pages=int(request.get("ocr_worker_recycle_pages", 25)),
                        memory_limit_mb=int(request.get("ocr_worker_memory_limit_mb", 2048)),
                    )
                service_result = self._ocr.analyze_selection(
                    source_path, rect, preferred_language=preferred, quality=quality,
                )
                ocr_result = service_result.ocr_result
                _write_cached_selection(cache_dir, cache_key, ocr_result)
                source_regions = service_result.final_regions
            source_language = source_language or ocr_result.language
            composed = compose_manual_region(source_regions)
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
            status = "translated"
            self._stage(TranslationRequestStatus.TRANSLATING, "Translating selected text")
            page = PageDialogue(
                source_language=source_language,
                target_language=request["target"],
                dialogue=[{
                    "id": "manual:1",
                    "text": composed.text,
                    "confidence": confidence,
                    "bbox": [[rect[0], rect[1]], [rect[2], rect[1]], [rect[2], rect[3]], [rect[0], rect[3]]],
                }],
                page_context="Manual user-selected text box. Translate this one selected manga bubble.",
            )
            page_result = TRANSLATION_RUNTIME.translate_page(page, request)
            translated_text = (
                str(page_result.translations[0].get("text", "")).strip()
                if page_result.translations else ""
            )
            if not translated_text:
                raise ValueError("The selected translation engine did not return usable text.")
            if not translated_text or "translation_unchanged" in review_reasons:
                raise ValueError("The selected text could not be translated reliably.")
            review_reasons = list(dict.fromkeys(review_reasons))
            if review_reasons:
                status = "review"
            self.succeeded.emit({
                "request_id": str(request.get("request_id") or ""),
                "project_id": request["project_id"], "image_id": request["image_id"],
                "image_index": request["image_index"], "id": str(uuid4()), "rect": rect,
                "source_polygons": [region["polygon"] for region in source_regions], "original_text": composed.text,
                "source_member_texts": [str(region.get("text", "")) for region in source_regions],
                "translated_text": translated_text, "ocr_confidence": confidence,
                "source_language": source_language, "direction": composed.direction,
                "status": status, "review_reasons": review_reasons,
                "suppressed_auto_group_indices": [],
            })
        except Exception as error:
            self.failed.emit({
                "project_id": request.get("project_id", ""), "image_id": request.get("image_id", ""),
                "image_index": request.get("image_index", -1), "message": str(error) or type(error).__name__,
            })


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
            return False
        typed_request = (
            request if isinstance(request, TranslationRequest)
            else TranslationRequest.from_legacy_manual(request)
        )
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

    @Slot(str, object)
    def _on_queue_completed(self, request_id: str, result: dict) -> None:
        if self._active.pop(request_id, None) is None:
            return
        self.busy_changed.emit(bool(self._active))
        self.succeeded.emit(result)

    @Slot(str, object)
    def _on_queue_failed(self, request_id: str, result: dict) -> None:
        request = self._active.pop(request_id, None)
        if request is None:
            return
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
