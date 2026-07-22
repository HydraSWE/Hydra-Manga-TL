"""Asynchronous OCR and translation for user-drawn text rectangles."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .layout import compose_manual_region
from .ocr import PaddleOCREngine, normalize_ocr_text, retry_suspicious_digit_regions
from .translation import JsonTranslationCache, build_translation_services, translate_regions


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


class _ManualRegionWorker(QObject):
    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._ocr = None
        self._translator = None
        self._localizer = None
        self._service_key = None

    @Slot(object)
    def process(self, request: dict) -> None:
        try:
            if self._ocr is None:
                self._ocr = PaddleOCREngine(["ch", "japan", "en"])
            service_key = (
                request.get("literal_provider", "marian"), request.get("localization_provider", "local"),
                request.get("localization_model", ""), tuple(sorted(request.get("glossary", {}).items())),
            )
            if service_key != self._service_key:
                self._translator, self._localizer = build_translation_services(
                    request.get("literal_provider", "marian"), request.get("localization_provider", "local"),
                    model=request.get("localization_model", ""), glossary=request.get("glossary", {}),
                )
                self._service_key = service_key
            rect = [int(value) for value in request["rect"]]
            source_language = request.get("source_language", "")
            preferred = {"Japanese": "japan", "Chinese": "ch", "Latin-script": "en"}.get(source_language)
            ocr_result = self._ocr.analyze_selection(Path(request["source_path"]), rect, preferred_language=preferred)
            source_language = source_language or ocr_result.language
            source_regions = ocr_result.to_dict()["regions"]
            for region in source_regions:
                region["text"] = normalize_ocr_text(str(region["text"]))
            source_regions = retry_suspicious_digit_regions(
                self._ocr, Path(request["source_path"]), source_regions, preferred or ocr_result.model_language,
            )
            composed = compose_manual_region(source_regions)
            if source_language == "Japanese" and sum(char.isascii() and char.isdigit() for char in composed.text) >= 3:
                raise ValueError("Japanese OCR still contains suspicious digits. Draw the text box slightly wider and try again.")
            if source_language == "Latin-script" and request["target"] == "en":
                raise ValueError("The selected text already appears to be English.")
            confidence = min(source_regions[index - 1]["confidence"] for index in composed.member_indices)
            translated = translate_regions([{
                "text": composed.text,
                "confidence": confidence,
                "polygon": [[0, 0], [rect[2] - rect[0], 0], [rect[2] - rect[0], rect[3] - rect[1]], [0, rect[3] - rect[1]]],
            }], source_language, request["target"], self._translator,
                localizer=self._localizer, style=request.get("localization_style", "Manga"),
                glossary=request.get("glossary", {}),
                constraints=[{"width": rect[2] - rect[0], "height": rect[3] - rect[1], "max_lines": request.get("max_lines", 3)}],
                cache=JsonTranslationCache(Path(request["cache_path"])),
            )[0]
            if not translated.translated_text or "translation_unchanged" in translated.review_reasons:
                raise ValueError("The selected text could not be translated reliably.")
            self.succeeded.emit({
                "project_id": request["project_id"], "image_id": request["image_id"],
                "image_index": request["image_index"], "id": str(uuid4()), "rect": rect,
                "source_polygons": [region["polygon"] for region in source_regions], "original_text": composed.text,
                "source_member_texts": [str(region.get("text", "")) for region in source_regions],
                "translated_text": translated.translated_text, "ocr_confidence": confidence,
                "source_language": source_language, "direction": composed.direction,
                "status": translated.status, "review_reasons": translated.review_reasons,
                "suppressed_auto_group_indices": [],
            })
        except Exception as error:
            self.failed.emit({
                "project_id": request.get("project_id", ""), "image_id": request.get("image_id", ""),
                "image_index": request.get("image_index", -1), "message": str(error) or type(error).__name__,
            })


class ManualRegionService(QObject):
    """Own a reusable background worker and its lazy-loaded local models."""
    _requested = Signal(object)
    succeeded = Signal(object)
    failed = Signal(object)
    busy_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._thread: QThread | None = None
        self._worker: _ManualRegionWorker | None = None
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        self._thread = QThread(self)
        self._worker = _ManualRegionWorker()
        self._worker.moveToThread(self._thread)
        self._requested.connect(self._worker.process)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._on_failed)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def submit(self, request: dict) -> bool:
        if self._busy:
            return False
        self._ensure_thread()
        self._busy = True
        self.busy_changed.emit(True)
        self._requested.emit(request)
        return True

    @Slot(object)
    def _on_succeeded(self, result: dict) -> None:
        self._busy = False
        self.busy_changed.emit(False)
        self.succeeded.emit(result)

    @Slot(object)
    def _on_failed(self, result: dict) -> None:
        self._busy = False
        self.busy_changed.emit(False)
        self.failed.emit(result)

    def shutdown(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
            self._thread = None
            self._worker = None
